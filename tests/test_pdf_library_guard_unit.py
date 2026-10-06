"""PyMuPDF (AGPL-3.0 / Artifex commercial licence) must never come back into the product.

The product reads PDFs with pypdfium2 (Apache-2.0; PDFium is Apache-2.0 or BSD-3-Clause), which
can ship in a closed commercial build. This guard fails the build if any code, script, test,
container file, CI file or dependency list names the old library or imports its modules.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THIS = Path(__file__).resolve()

# the old library's package, import and project names (built from parts so this file is clean)
_NAMES = ("py" + "mupdf", "f" + "itz", "mu" + "pdf")
FORBIDDEN = re.compile(r"(?i)\b(" + "|".join(_NAMES) + r")\b")
CODE_GLOBS = (
    "src/**/*.py",
    "scripts/**/*",
    "tests/**/*",
    "infra/**/*",
    ".github/**/*",
    "pyproject.toml",
    "Makefile",
    "requirements*.txt",
    "alembic.ini",
    ".env*",
)


def _files() -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in CODE_GLOBS:
        for path in ROOT.glob(pattern):
            if path.is_file() and path.resolve() != THIS and "__pycache__" not in path.parts:
                seen[path] = None
    return list(seen)


def test_the_scan_covers_the_expected_files():
    names = {p.relative_to(ROOT).as_posix() for p in _files()}
    for expected in (
        "src/cdi_adapter/ingest/pages.py",
        "scripts/make_sample_docs.py",
        "tests/helpers.py",
        "pyproject.toml",
        "infra/compose/Dockerfile",
    ):
        assert expected in names, expected


def test_no_code_config_or_test_names_the_old_pdf_library():
    hits = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary fixture
        for number, line in enumerate(text.splitlines(), start=1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {line.strip()[:80]}")
    assert not hits, "old PDF library referenced:\n" + "\n".join(hits)


def test_dependencies_declare_pypdfium2_and_not_the_old_library():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        declared += extra
    names = {re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0].lower().replace("_", "-") for d in declared}
    assert "pypdfium2" in names
    assert not {n for n in names if FORBIDDEN.fullmatch(n)}


def test_pypdfium2_is_pinned_below_the_next_major():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    spec = next(d for d in project["dependencies"] if d.lower().startswith("pypdfium2"))
    assert ">=" in spec and "<6" in spec.replace(" ", "")


def test_pages_module_uses_pypdfium2():
    source = (ROOT / "src/cdi_adapter/ingest/pages.py").read_text(encoding="utf-8")
    assert "import pypdfium2 as pdfium" in source
