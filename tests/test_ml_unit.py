"""E0-S4 unit: schemas, extract_json, repair_payload, validate_schema, vlm_json, stub, gateway."""

from __future__ import annotations

import base64
import json
import re
import weakref

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from cdi_adapter.config import Settings
from cdi_adapter.ml import client as mc
from cdi_adapter.mlserve.app import create_app
from cdi_adapter.mlserve.backends import HFBackend, StubBackend, make_backend

EXTRACTION = ["prescription.v3", "lab_report.v3", "vitals.v3", "opd_note.v3",
              "discharge_summary.v3", "radiology.v3"]  # fmt: skip
AGENT = [
    "med_agent_recall.v1",
    "med_agent_choice.v1",
    "med_agent_fit.v1",
    "med_agent_verify.v1",
]  # the medication agent (E12-S4)

# ---------------------------------------------------------------- schemas + registry


def test_all_eight_schemas_present_and_valid():
    assert set(mc.schema_names()) == {"classification.v1", "common.defs", *EXTRACTION, *AGENT}
    for name in mc.schema_names():
        schema = mc.load_schema(name)
        Draft202012Validator.check_schema(schema)
        assert schema["$id"] == f"cdi:{name}"


@pytest.mark.parametrize("name", EXTRACTION)
def test_patient_is_never_root_required(name):
    assert "patient" not in mc.load_schema(name).get("required", [])


@pytest.mark.parametrize("name", ["classification.v1", *EXTRACTION, *AGENT])
def test_bundled_schema_has_no_refs(name):
    assert "$ref" not in json.dumps(mc.bundled_schema(name))


MINIMAL_VALID = {
    "prescription.v3": {"medications": [{"drug_name": "Tab Metformin", "confidence": 0.9}],
                        "confidence": 0.9},
    "lab_report.v3": {"tests": [{"name": "HbA1c", "value": 7.8, "confidence": 0.9}],
                      "confidence": 0.9},
    "vitals.v3": {"vitals": [{"name": "pulse", "value": "78", "confidence": 0.9}],
                  "confidence": 0.9},
    "opd_note.v3": {"confidence": 0.9},
    "discharge_summary.v3": {"confidence": 0.9},
    "radiology.v3": {"findings_list": ["Mild cardiomegaly"], "confidence": 0.9},
    "classification.v1": {"doc_type": "prescription", "confidence": 0.95},
}  # fmt: skip


@pytest.mark.parametrize("name", list(MINIMAL_VALID))
def test_minimal_payload_validates(name):
    assert mc.validate_schema(MINIMAL_VALID[name], name) == []


@pytest.mark.parametrize("name", list(MINIMAL_VALID))
def test_missing_confidence_or_stray_key_is_invalid(name):
    bad = dict(MINIMAL_VALID[name])
    bad.pop("confidence")
    bad["surprise"] = 1
    errors = mc.validate_schema(bad, name)
    assert any("confidence" in e for e in errors)
    assert any("surprise" in e for e in errors)


def test_relaxations_from_architecture_s4():
    rx = {
        "patient": {"name": "Anjali Das", "sex": "Male"},  # free-text sex
        "medications": [{"drug_name": "Tab X", "dose": 150, "strength": "500 mg",
                         "confidence": 0.9, "extra_key": "allowed"}],
        "vitals": [{"name": "bp", "value": "138/86", "confidence": 0.9}],  # bare string
        "confidence": 0.9,
    }  # fmt: skip
    assert mc.validate_schema(rx, "prescription.v3") == []
    rad = {"findings_list": ["plain string", {"text": "obj", "code": "1"}],
           "impression_concepts": ["x"], "diagnoses": ["y"], "confidence": 0.8}  # fmt: skip
    assert mc.validate_schema(rad, "radiology.v3") == []


def test_classification_rejects_unknown_doc_type():
    assert mc.validate_schema({"doc_type": "poem", "confidence": 0.9}, "classification.v1")


# ---------------------------------------------------------------- extract_json


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        'Sure! Here is the JSON:\n```json\n{"a": 1}\n```\nHope it helps.',
        '```\n{"a": 1}\n```',
        'prefix {"a": 1} suffix {"b": 2}',
        'noise { not json } then {"a": 1}',
    ],
)
def test_extract_json_tolerates_fences_and_prose(text):
    assert mc.extract_json(text) == {"a": 1}


def test_extract_json_nested():
    assert mc.extract_json('x {"a": {"b": [1, {"c": 2}]}} y') == {"a": {"b": [1, {"c": 2}]}}


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2, 3]", "{broken"])
def test_extract_json_raises_when_no_object(text):
    with pytest.raises(ValueError):
        mc.extract_json(text)


# ---------------------------------------------------------------- repair_payload


def test_repair_rehomes_wrong_key_and_fills_confidence_and_drops_strays():
    raw = {"medicines": [{"name": "Tab Metformin 500 mg"}], "garbage": 1}
    out = mc.repair_payload(raw, "prescription.v3")
    assert out == {
        "medications": [{"drug_name": "Tab Metformin 500 mg", "confidence": mc.FILLED_CONFIDENCE}],
        "confidence": mc.FILLED_CONFIDENCE,
    }
    assert mc.validate_schema(out, "prescription.v3") == []


def test_repair_wraps_single_object_and_converts_string_items():
    out = mc.repair_payload(
        {"tests": {"name": "HbA1c", "value": 7.8}, "confidence": 0.9}, "lab_report.v3"
    )
    assert out["tests"] == [{"name": "HbA1c", "value": 7.8, "confidence": mc.FILLED_CONFIDENCE}]
    out = mc.repair_payload({"medications": ["Tab Amlodipine 5 mg"], "confidence": 0.9},
                            "prescription.v3")  # fmt: skip
    assert out["medications"][0]["drug_name"] == "Tab Amlodipine 5 mg"


CONFIDENCE_CASES = [(95, 0.95), ("90%", 0.9), (0.7, 0.7), (-1, 0.0),
                    ("high", mc.FILLED_CONFIDENCE), (True, 0.5)]  # fmt: skip


@pytest.mark.parametrize("given,expected", CONFIDENCE_CASES)
def test_repair_normalizes_confidence(given, expected):
    out = mc.repair_payload({"tests": [], "confidence": given}, "lab_report.v3")
    assert out["confidence"] == pytest.approx(expected)


def test_repair_never_invents_clinical_values():
    out = mc.repair_payload({"confidence": 0.9}, "prescription.v3")
    assert "medications" not in out  # required list stays missing -> invalid -> held
    assert mc.validate_schema(out, "prescription.v3")


def test_repair_keeps_mixed_string_or_object_lists_intact():
    out = mc.repair_payload({"findings_list": ["a", {"text": "b"}], "confidence": 0.9},
                            "radiology.v3")  # fmt: skip
    assert out["findings_list"] == ["a", {"text": "b"}]


def test_repair_null_list_becomes_empty():
    out = mc.repair_payload({"medications": None, "confidence": 0.9}, "prescription.v3")
    assert out["medications"] == []


# ---------------------------------------------------------------- vlm_json retry / _partial


class ScriptedClient(mc.MLClient):
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def generate(self, prompt, image_png=None, *, max_tokens=1024, json_schema=None):
        self.prompts.append(prompt)
        assert json_schema and json_schema["$id"].startswith("cdi:")
        return self.replies.pop(0)


def test_vlm_json_valid_first_time():
    c = ScriptedClient([json.dumps(MINIMAL_VALID["lab_report.v3"])])
    out = c.vlm_json(None, "p", "lab_report.v3", retries=1)
    assert out == MINIMAL_VALID["lab_report.v3"] and len(c.prompts) == 1
    assert not mc.is_partial(out)


def test_vlm_json_retries_once_then_succeeds():
    c = ScriptedClient(["not json at all", json.dumps(MINIMAL_VALID["lab_report.v3"])])
    out = c.vlm_json(None, "p", "lab_report.v3", retries=1)
    assert not mc.is_partial(out) and len(c.prompts) == 2
    assert "previous answer was invalid" in c.prompts[1]


def test_vlm_json_returns_partial_instead_of_raising():
    c = ScriptedClient(["garbage", "still garbage"])
    out = c.vlm_json(None, "p", "lab_report.v3", retries=1)
    assert out["_partial"] is True and out["_errors"] and len(c.prompts) == 2


def test_vlm_json_partial_keeps_best_repaired_object():
    bad = json.dumps({"tests": [{"value": 5}], "confidence": 0.9})  # test name missing
    c = ScriptedClient([bad, bad])
    out = c.vlm_json(None, "p", "lab_report.v3", retries=1)
    assert out["_partial"] is True and out["tests"][0]["value"] == 5


def test_vlm_json_default_retries_from_settings(monkeypatch):
    from cdi_adapter.config import get_settings

    monkeypatch.setenv("CDI_EXTRACT_RETRIES", "0")
    get_settings.cache_clear()
    try:
        c = ScriptedClient(["garbage"])
        assert c.vlm_json(None, "p", "lab_report.v3")["_partial"] and len(c.prompts) == 1
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------- stub responder

RX_DOC = """<document>
[b1] CareFlow Polyclinic
[b2] Name: Anjali Das   Age/Sex: 48 Y / F
[b3] Date: 12/08/2026
[b4] Diagnosis: Type 2 diabetes mellitus, Hypertension
[b5] Rx
[b6] Tab Metformin 500 mg 1-0-1 x 30 days
[b7] Tab Amlodipine 5 mg OD x 30 days
[b8] BP: 138/86 mmHg   Weight: 72 kg
[b9] Advice: low salt diet
</document>"""


def test_stub_classifies_prescription_and_ignores_instruction_text():
    prompt = "Types: lab_report radiology discharge summary vitals\n" + RX_DOC
    out = json.loads(StubBackend().generate(prompt, json_schema={"$id": "cdi:classification.v1"}))
    assert out["doc_type"] == "prescription"
    assert mc.validate_schema(out, "classification.v1") == []


def test_stub_prescription_extraction_is_schema_valid_and_evidenced():
    out = mc.StubMLClient().vlm_json(None, RX_DOC, "prescription.v3", retries=0)
    assert not mc.is_partial(out)
    names = [m["drug_name"] for m in out["medications"]]
    assert names == ["Tab Metformin", "Tab Amlodipine"]
    assert out["medications"][0]["frequency"] == "1-0-1"
    assert out["medications"][0]["strength"] == {"value": 500.0, "unit": "mg"}
    assert out["medications"][0]["evidence"] == ["b6"]
    assert [d["text"] for d in out["diagnoses"]] == ["Type 2 diabetes mellitus", "Hypertension"]
    assert {v["name"] for v in out["vitals"]} == {"blood_pressure", "body_weight"}
    assert out["patient"]["name"] == "Anjali Das"


@pytest.mark.parametrize(
    "schema,doc",
    [
        ("lab_report.v3", "[b1] HbA1c 7.8 % 4.0-5.6 H\n[b2] Creatinine 1.1 mg/dL 0.6-1.3"),
        ("vitals.v3", "[b1] Pulse: 78\n[b2] Allergy: Penicillin"),
        ("opd_note.v3", "[b1] C/O: fever, cough\n[b2] Diagnosis: URTI"),
        ("discharge_summary.v3", "[b1] Final Diagnosis: CAD\n[b2] Procedure: PTCA to LAD"),
        ("radiology.v3", "[b1] Chest X-ray PA view\n[b2] Findings: clear lung fields\n"
                         "[b3] Impression: normal study"),
    ],
)  # fmt: skip
def test_stub_payloads_validate_for_every_extraction_schema(schema, doc):
    out = mc.StubMLClient().vlm_json(None, f"<document>\n{doc}\n</document>", schema, retries=0)
    assert not mc.is_partial(out), out


def test_stub_lab_values():
    doc = "<document>\n[b1] HbA1c 7.8 % 4.0-5.6 H\n[b2] Name: X\n</document>"
    out = mc.StubMLClient().vlm_json(None, doc, "lab_report.v3", retries=0)
    assert out["tests"] == [{"name": "HbA1c", "value": 7.8, "unit": "%", "ref_range": "4.0-5.6",
                             "flag": "H", "evidence": ["b1"], "confidence": 0.97}]  # fmt: skip


def test_stub_without_schema_is_transcription():
    assert StubBackend().generate("<document>\nline one\nline two\n</document>") == (
        "line one\nline two"
    )


def test_get_client_by_backend(monkeypatch):
    from cdi_adapter.config import get_settings

    for backend, cls in (("stub", mc.StubMLClient), ("hf", mc.HttpMLClient)):
        monkeypatch.setenv("CDI_MLSERVE_BACKEND", backend)
        get_settings.cache_clear()
        assert isinstance(mc.get_client(), cls)
    get_settings.cache_clear()


# ---------------------------------------------------------------- HTTP client <-> gateway contract


def test_http_client_speaks_gateway_contract():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.url.path == "/vlm/generate"
        return httpx.Response(200, json={"text": "{}", "backend": "hf", "model": "m", "usage": {}})

    c = mc.HttpMLClient("http://gw", transport=httpx.MockTransport(handler))
    assert c.generate("hello", b"\x89PNG", max_tokens=7, json_schema={"$id": "cdi:x"}) == "{}"
    assert seen == {"prompt": "hello", "image_b64": base64.b64encode(b"\x89PNG").decode(),
                    "max_tokens": 7, "json_schema": {"$id": "cdi:x"}}  # fmt: skip
    assert c.model == "m"


# ---------------------------------------------------------------- gateway API


@pytest.fixture
def gateway():
    return TestClient(create_app(StubBackend()))


def test_gateway_generate(gateway):
    body = {"prompt": RX_DOC, "image_b64": base64.b64encode(b"img").decode(), "max_tokens": 64,
            "json_schema": {"$id": "cdi:classification.v1"}}  # fmt: skip
    r = gateway.post("/vlm/generate", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["backend"] == "stub" and data["model"] == "stub-keyword-responder"
    assert json.loads(data["text"])["doc_type"] == "prescription"
    assert set(data["usage"]) == {"prompt_chars", "output_chars", "seconds"}


@pytest.mark.parametrize(
    "body",
    [{"prompt": ""}, {"prompt": "x", "max_tokens": 0}, {"prompt": "x", "image_b64": "%%%"}, {}],
)
def test_gateway_rejects_bad_requests(gateway, body):
    assert gateway.post("/vlm/generate", json=body).status_code == 422


def test_gateway_healthz(gateway):
    from cdi_adapter.config import get_settings

    s = get_settings()  # the host's .env may configure hf and a registered version (Colab, pod)
    h = gateway.get("/healthz").json()
    assert h == {"status": "ok", "backend": "stub", "model": "stub-keyword-responder",
                 "version": s.model_version or None, "device": "cpu", "loaded": True,
                 "configured_backend": s.mlserve_backend}  # fmt: skip


# ---------------------------------------------------------------- hf backend logic (no GPU)


class OutOfMemoryError(RuntimeError):
    """Same class name torch.cuda raises."""


def _settings():
    return Settings(_env_file=None, mlserve_backend="hf")


def test_hf_lazy_loads_once_on_first_request():
    loads = []
    be = HFBackend(_settings(), loader=lambda mid, s: loads.append(mid) or ("M", "P"),
                   runner=lambda m, p, prompt, img, n: f"{m}:{prompt}:{n}")  # fmt: skip
    assert not be.loaded
    assert be.generate("hi", max_tokens=5) == "M:hi:5"
    be.generate("again")
    assert be.loaded and loads == ["Qwen/Qwen2.5-VL-7B-Instruct"]


def test_hf_falls_back_7b_to_3b_on_oom():
    def loader(mid, s):
        if "7B" in mid:
            raise OutOfMemoryError("CUDA out of memory")
        return ("M3", "P3")

    be = HFBackend(_settings(), loader=loader, runner=lambda *a: "ok")
    assert be.generate("x") == "ok"
    assert be.model == "Qwen/Qwen2.5-VL-3B-Instruct"


def test_hf_fallback_loads_only_after_the_failed_primary_is_released():
    """Colab T4, 2026-09-26: the half-built 7B was still held by the caught error's traceback
    while the 3B loaded, so the 3B ran out of memory too and no model was served at all."""

    class HalfLoaded:  # the 7B weights materialised before the OOM
        pass

    held = []

    def loader(mid, s):
        if "7B" in mid:
            weights = HalfLoaded()
            weights.graph = weights  # module graphs hold reference cycles
            held.append(weakref.ref(weights))
            raise OutOfMemoryError("CUDA out of memory")
        assert held[0]() is None, "the failed 7B is still alive while the 3B loads"
        return ("M3", "P3")

    be = HFBackend(_settings(), loader=loader, runner=lambda *a: "ok")
    assert be.generate("x") == "ok"
    assert be.loaded and be.model == "Qwen/Qwen2.5-VL-3B-Instruct"


def test_hf_loads_bf16_by_default_and_int8_only_when_asked(monkeypatch):
    import sys
    from types import SimpleNamespace

    # This unit checks the loader contract; GPU libraries belong to the serving environment.
    class QuantConfig:
        def __init__(self, *, load_in_8bit):
            self.load_in_8bit = load_in_8bit
            self.load_in_4bit = False

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(bfloat16="torch.bfloat16"))
    monkeypatch.setitem(
        sys.modules, "transformers", SimpleNamespace(BitsAndBytesConfig=QuantConfig)
    )
    from cdi_adapter.mlserve.backends import _load_kwargs

    plain = _load_kwargs("Qwen/Qwen2.5-VL-7B-Instruct", _settings())
    assert str(plain["torch_dtype"]) == "torch.bfloat16" and "quantization_config" not in plain
    eight = Settings(_env_file=None, mlserve_backend="hf", vlm_quantize="8bit")
    q = _load_kwargs("Qwen/Qwen2.5-VL-7B-Instruct", eight)
    assert q["quantization_config"].load_in_8bit and not q["quantization_config"].load_in_4bit
    assert {k: v for k, v in q.items() if k != "quantization_config"} == plain
    with pytest.raises(ValueError):
        Settings(_env_file=None, vlm_quantize="4bit")  # only what a host has been checked with


def test_start_mlserve_installs_the_pinned_bitsandbytes_only_for_8bit():
    from pathlib import Path

    runpod = Path(__file__).resolve().parents[1] / "infra" / "runpod"
    start = (runpod / "start_mlserve.sh").read_text(encoding="utf-8")
    pins = (runpod / "versions.env").read_text(encoding="utf-8")
    assert '[ "${CDI_VLM_QUANTIZE:-}" = "8bit" ]' in start
    assert "--no-deps --require-hashes" in start
    assert "bitsandbytes==$BITSANDBYTES_VERSION --hash=sha256:$BITSANDBYTES_SHA256" in start
    assert "\nBITSANDBYTES_VERSION=0.50.2\n" in pins
    assert re.search(r"\nBITSANDBYTES_SHA256=[0-9a-f]{64}\n", pins)


def test_hf_non_oom_load_error_is_raised():
    def loader(mid, s):
        raise FileNotFoundError("weights missing")

    with pytest.raises(FileNotFoundError):
        HFBackend(_settings(), loader=loader, runner=lambda *a: "").generate("x")


def test_make_backend_follows_settings():
    assert isinstance(make_backend(Settings(_env_file=None, mlserve_backend="stub")), StubBackend)
    assert isinstance(make_backend(_settings()), HFBackend)


def test_stub_keeps_a_medication_when_ocr_joins_form_name_and_strength():
    from cdi_adapter.mlserve.backends import _medications

    meds = _medications([("b7", "TabAmlodipine5mg ODx30days")])
    assert len(meds) == 1
    assert meds[0]["drug_name"] == "Tab Amlodipine"
    assert meds[0]["strength"] == {"value": 5.0, "unit": "mg"}
    assert meds[0]["evidence"] == ["b7"]
    assert _medications([("b1", "Table of results")]) == []
