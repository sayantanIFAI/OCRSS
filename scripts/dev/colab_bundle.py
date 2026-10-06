"""Package the verified local tree for a verification host (owner, 2026-09-25: "keep Colab
synchronized with the verified local code"; no more hand-patching the host per test).

Builds ``cdi-src.tar.gz``: every tracked and untracked-not-ignored file of the working tree (the
same set the Colab notebook's Step 2 unpacks), plus ``MANIFEST.sha256`` at its root. On the host,
``infra/colab/sync.sh`` checks every file against that manifest, applies the tree (removing files
the previous bundle had and this one does not), migrates the database and restarts the site, so
the host runs exactly this tree. Run it after the local CI gate is green.

A text file with CRLF line ends is refused (bash on the host fails on ``$'\\r'``; Windows editors
and ``Path.write_text`` make them).

    python scripts/dev/colab_bundle.py [--out C:/Users/<you>/Desktop/cdi-src.tar.gz]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "MANIFEST.sha256"
BINARY = {".png", ".jpg", ".jpeg", ".pdf", ".gz", ".tgz", ".zip", ".ico", ".woff", ".woff2"}


def tree_files(root: Path = ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "--", ".", ":!.claude"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return sorted(f for f in out.splitlines() if f and (root / f).is_file())


def crlf_files(root: Path, files: list[str]) -> list[str]:
    bad = []
    for f in files:
        if Path(f).suffix.lower() in BINARY:
            continue
        data = (root / f).read_bytes()
        if b"\0" not in data[:8192] and b"\r\n" in data:
            bad.append(f)
    return bad


def manifest(root: Path, files: list[str]) -> str:
    return "".join(f"{hashlib.sha256((root / f).read_bytes()).hexdigest()}  {f}\n" for f in files)


def build(root: Path, files: list[str], out: Path) -> str:
    """Write the bundle; return its id (the first 12 hex of the manifest's sha256)."""
    text = manifest(root, files)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".partial")
    with tarfile.open(tmp, "w:gz") as tar:
        for f in files:
            tar.add(root / f, arcname=f, recursive=False)
        info = tarfile.TarInfo(MANIFEST)
        info.size = len(text.encode())
        tar.addfile(info, io.BytesIO(text.encode()))
    tmp.replace(out)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path.home() / "Desktop" / "cdi-src.tar.gz")
    args = ap.parse_args(argv)
    files = tree_files()
    bad = crlf_files(ROOT, files)
    if bad:
        print("refused: CRLF line ends (convert to LF first):", *bad, sep="\n  ")
        return 1
    bundle_id = build(ROOT, files, args.out)
    size = args.out.stat().st_size
    print(f"bundle {bundle_id}: {len(files)} files, {size / 1e6:.2f} MB -> {args.out}")
    print("on the host: upload it to /content, then  bash /content/cdi/infra/colab/sync.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
