"""E11-S4 with the real models (Presidio + spaCy en_core_web_lg + obi/deid_roberta_i2b2).

Opt-in: CDI_TEST_DEID_MODELS=1 with the ``deid`` extra, the spaCy model and the pinned NER model in
the Hugging Face cache (``python -m cdi_adapter.deid fetch-models``). ~2 GB of models.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("CDI_TEST_DEID_MODELS"),
    reason="set CDI_TEST_DEID_MODELS=1 (needs the deid models)",
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

SAMPLE = """Name: Meenakshi Subramaniam   S/o or W/o: Rajendra Subramaniam
DOB: 14/03/1978   Age/Sex: 48 Y / F   ABHA No: 91-4821-7730-1956   ABHA Address: meenakshi512@abdm
Aadhaar: 2341 2341 2346   PAN: ABCPD1234F   Mobile: +91 98300 12345   Email: meenakshi.s@example.in
Address: 42, Lake Gardens, Kolkata   PIN: 700045   UHID: UH482913
Consultant: Dr. Gopinath Raghavan
Diagnosis: Dengue fever. Platelet count 88000 /uL for 3 days. Tab Paracetamol 650 mg SOS."""


def test_every_identifier_goes_and_the_clinical_content_stays() -> None:
    from cdi_adapter.deid.engine import DocumentKey, deidentify_text

    clean, spans = deidentify_text(SAMPLE, DocumentKey(shift_days=40))
    for gone in ("Meenakshi", "Subramaniam", "Rajendra", "14/03/1978", "4821", "meenakshi512",
                 "2341", "ABCPD1234F", "98300", "Lake Gardens", "Kolkata", "700045", "UH482913",
                 "Gopinath", "Raghavan"):  # fmt: skip
        assert gone not in clean, gone
    for kept in ("Dengue fever", "Platelet count 88000 /uL", "for 3 days", "650 mg SOS",
                 "ABHA No:", "UHID:", "DOB:", "Age/Sex: 48 Y / F"):  # fmt: skip
        assert kept in clean, kept
    assert "DOB: 23/04/1978" in clean  # shifted by the document's own offset, same format
    assert {"[ABHA]", "[ABHA-ADDRESS]", "[AADHAAR]", "[PAN]", "[PHONE]", "[EMAIL]", "[PIN]",
            "[ID]"} <= set(__import__("re").findall(r"\[[A-Z-]+\]", clean))  # fmt: skip


def test_a_red_team_attempt_on_a_sample_fails() -> None:
    """The E11-S4 acceptance test, on a small sample (scripts/deid_redteam.py runs larger ones)."""
    import deid_redteam

    report = deid_redteam.run(patients=2, seed=5)
    assert report["control_succeeded"], "the attack must find identifiers in the originals"
    assert report["leaks"] == 0, report["results"]
    assert report["passed"]
