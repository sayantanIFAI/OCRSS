"""Secret scan of the working tree (and git history, when there is a repository) — backlog E1-S5.

Fails (exit 1) on:
  * real-credential patterns anywhere: RunPod / Hugging Face / GitHub / AWS keys, private keys,
    and connection URLs whose password is not a known dev default or a placeholder;
  * the well-known dev defaults (`cdi` DB password, `minioadmin`) outside the files that are
    explicitly local-dev / CI-only (DEV_SCOPED) — e.g. in the pod config or pod scripts.

Findings are printed masked; a secret value is never echoed in full.

    python scripts/secret_scan.py [--root DIR]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git", ".venv", ".tools", ".mypy_cache", ".ruff_cache", ".pytest_cache", "__pycache__",
    "node_modules", "data", "logs", "htmlcov", "build", "dist",
}  # fmt: skip
TEXT_SUFFIXES = {
    ".py", ".sh", ".ps1", ".md", ".txt", ".toml", ".ini", ".cfg", ".yml", ".yaml", ".json",
    ".sql", ".env", ".example", ".runpod", ".mako", ".html", ".css", ".js", "",
}  # fmt: skip

# Files whose dev defaults are intended: local dev, the throwaway test/CI databases, and docs.
DEV_SCOPED = (
    ".env.example",
    "Makefile",
    "README.md",
    "alembic.ini",
    "src/cdi_adapter/config.py",
    "src/cdi_adapter/secretstore.py",
    "scripts/secret_scan.py",
    "scripts/dev/",
    "tests/",
    "docs/",
    ".github/workflows/",
    # compose alt-deploy files (Phase 0): not a deployment path — the fallback host is native
    # (E1-S3, owner decision 2026-09-21), so its dev defaults never reach a shared host.
    "infra/compose/",
)

PATTERNS: dict[str, re.Pattern[str]] = {
    "runpod-api-key": re.compile(r"\brpa_[A-Za-z0-9]{20,}"),
    "huggingface-token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
URL_CRED = re.compile(r"\b[a-z][a-z0-9+.-]*://([^:/\s@'\"]*):([^@\s/'\"]+)@")
DEV_PASSWORDS = {"cdi", "minioadmin"}
# shell/f-string templates, <angle> placeholders and masks are not credentials
PLACEHOLDER = re.compile(
    r"^(\$\{?[A-Za-z_].*|\{.*\}|<.*>|\*+|x+|\.\.\.|PASSWORD|password|pw|pass)$"
)
DEV_DEFAULT = re.compile(r"minioadmin|://cdi:cdi@|PASSWORD '?cdi'?")


@dataclass(frozen=True)
class Finding:
    where: str
    line: int
    kind: str
    masked: str

    def __str__(self) -> str:
        return f"{self.where}:{self.line}: {self.kind}: {self.masked}"


def mask(s: str) -> str:
    return s if len(s) <= 4 else s[:4] + f"...({len(s)} chars)"


def _dev_scoped(rel: str) -> bool:
    return any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in DEV_SCOPED)


def scan_text(rel: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    dev_ok = _dev_scoped(rel)
    for n, line in enumerate(text.splitlines(), 1):
        for kind, pat in PATTERNS.items():
            for m in pat.finditer(line):
                out.append(Finding(rel, n, kind, mask(m.group(0))))
        for m in URL_CRED.finditer(line):
            pw = m.group(2)
            if PLACEHOLDER.match(pw) or pw in DEV_PASSWORDS:
                continue
            out.append(Finding(rel, n, "password-in-url", mask(pw)))
        dev = None if dev_ok else DEV_DEFAULT.search(line)
        if dev:
            out.append(Finding(rel, n, "dev-default-credential", mask(dev.group(0))))
    return out


def iter_files(root: Path) -> Iterator[Path]:
    for p in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.is_file() and (p.suffix in TEXT_SUFFIXES or p.name.startswith(".env")):
            yield p


def scan_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for p in iter_files(root):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings += scan_text(p.relative_to(root).as_posix(), text)
    return findings


def scan_history(root: Path) -> list[Finding] | None:
    """Every added line in every commit; None when `root` is not a git repository."""
    if not (root / ".git").exists():
        return None
    log = subprocess.run(
        ["git", "log", "--all", "-p", "--unified=0", "--no-color", "--format=commit %H"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout  # fmt: skip
    findings: list[Finding] = []
    commit, path = "?", "?"
    for line in log.splitlines():
        if line.startswith("commit "):
            commit = line.split()[1][:10]
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            for f in scan_text(path, line[1:]):
                findings.append(Finding(f"{commit}:{f.where}", f.line, f.kind, f.masked))
    return findings


def report(findings: Iterable[Finding], history: list[Finding] | None) -> int:
    tree = list(findings)
    for f in tree:
        print(f"FAIL {f}")
    if history is None:
        print("[secret-scan] git history: not a git repository - nothing to scan")
    else:
        for f in history:
            print(f"FAIL history {f}")
    bad = len(tree) + len(history or [])
    print(f"[secret-scan] {'CLEAN' if not bad else f'{bad} finding(s)'}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    args = ap.parse_args(argv)
    return report(scan_tree(args.root), scan_history(args.root))


if __name__ == "__main__":
    sys.exit(main())
