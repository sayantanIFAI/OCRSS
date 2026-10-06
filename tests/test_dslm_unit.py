"""E2-S6: AWQ/INT4 quantization + serving benchmark tooling for the fine-tuned DSLM.

llm-compressor and vLLM run on the GPU host (Linux); here the recipe, the corpus, the benchmark
arithmetic and the production backend's new hooks are checked, the engine replaced by a fake
with vLLM's output shape (``prompt_token_ids``, ``outputs[0].token_ids``, ``vllm_config``)."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cdi_adapter.config import Settings
from cdi_adapter.dslm import bench, corpus, quantize
from cdi_adapter.mlserve.backends import VLLMBackend

ROOT = Path(__file__).resolve().parents[1]
SAMPLES: Path


@pytest.fixture(scope="module", autouse=True)
def generated_samples(tmp_path_factory):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from make_sample_docs import generate

    global SAMPLES
    SAMPLES = tmp_path_factory.mktemp("dslm-samples")
    generate(SAMPLES)


RUNPOD = ROOT / "infra" / "runpod"


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, mlserve_backend="vllm", **kw)  # type: ignore[call-arg]


# ------------------------------------------------------------------ corpus


def _fake_ocr(lines: list[str]):
    return lambda png: [{"id": f"u{i}", "text": t} for i, t in enumerate(lines)]


RX_LINES = ["City Clinic - Prescription", "Name: Test Patient Age: 40 Y", "Date: 01/08/2026",
            "Rx", "Tab Metformin 500 mg 1-0-1 x 30 days", "Tab Amlodipine 5 mg OD x 30 days",
            "Tab Atorvastatin 10 mg HS x 30 days after food", "Diagnosis: Type 2 diabetes",
            "Advice: review after one month with fasting sugar"]  # fmt: skip


def test_a_request_is_the_one_the_pipeline_sends() -> None:
    from cdi_adapter.extract.prompt import build_extraction_prompt
    from cdi_adapter.ml.client import bundled_schema

    raw = (SAMPLES / "prescription_00.pdf").read_bytes()
    req, png = corpus.build_request(raw, "prescription_00.pdf", ocr=_fake_ocr(RX_LINES))
    assert png.startswith(b"\x89PNG")
    assert req.doc_type == "prescription" and req.schema == "prescription.v3"
    assert req.max_tokens == 2600  # S4's per-type budget
    blocks = [{"id": f"u{i}", "text": t} for i, t in enumerate(RX_LINES)]
    assert req.prompt == build_extraction_prompt("prescription", blocks)[0]
    assert req.json_schema == bundled_schema("prescription.v3")
    assert "$ref" not in json.dumps(req.json_schema)  # the gateway gets it inlined
    assert req.blocks == len(RX_LINES) and len(req.source_sha256) == 64


def test_an_unsure_classification_must_be_labelled() -> None:
    raw = (SAMPLES / "prescription_00.pdf").read_bytes()
    with pytest.raises(ValueError, match="--label"):
        corpus.build_request(raw, "x.pdf", ocr=_fake_ocr(["???", "..."]))
    req, _ = corpus.build_request(raw, "x.pdf", doc_type="vitals_sheet", ocr=_fake_ocr(["???"]))
    assert req.schema == "vitals.v3" and req.max_tokens == 1200
    with pytest.raises(ValueError, match="no extraction schema"):
        corpus.build_request(raw, "x.pdf", doc_type="invoice", ocr=_fake_ocr(["x"]))


def _corpus(tmp_path: Path, n: int = 3, data_class: str = "synthetic") -> Path:
    raw = (SAMPLES / "prescription_00.pdf").read_bytes()
    items = []
    types = ["prescription", "lab_report", "vitals_sheet"]
    for i in range(n):
        req, png = corpus.build_request(raw, f"doc{i}.pdf", doc_type=types[i % 3],
                                        ocr=_fake_ocr(RX_LINES))  # fmt: skip
        items.append((req, png))
    out = tmp_path / "corpus"
    corpus.write_corpus(out, items, data_class=data_class)
    return out


def test_the_corpus_round_trips_and_its_digest_covers_the_images(tmp_path: Path) -> None:
    out = _corpus(tmp_path)
    doc = corpus.read_corpus(out)
    assert doc["data_class"] == "synthetic" and len(doc["requests"]) == 3
    assert all(Path(r["image_path"]).is_file() for r in doc["requests"])
    d1 = corpus.corpus_digest(out)
    img = Path(doc["requests"][0]["image_path"])
    img.write_bytes(img.read_bytes() + b"x")
    assert corpus.corpus_digest(out) != d1  # a changed page changes what was calibrated on


def test_only_synthetic_or_deidentified_documents_enter_a_corpus(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="de-identified"):
        _corpus(tmp_path, data_class="real")
    out = _corpus(tmp_path)
    doc = json.loads((out / corpus.CORPUS_FILE).read_text(encoding="utf-8"))
    doc["data_class"] = "identified"
    (out / corpus.CORPUS_FILE).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="not allowed"):
        corpus.read_corpus(out)


def test_corpus_cli_builds_from_the_real_samples_with_rapidocr(tmp_path: Path) -> None:
    pytest.importorskip("rapidocr_onnxruntime")
    out = tmp_path / "c"
    rc = corpus.main(
        [
            "build",
            "--out",
            str(out),
            "--data-class",
            "synthetic",
            str(SAMPLES / "lab_report_01.pdf"),
            str(SAMPLES / "vitals_sheet_02.pdf"),
        ]
    )
    assert rc == 0
    doc = corpus.read_corpus(out)
    assert [r["doc_type"] for r in doc["requests"]] == ["lab_report", "vitals_sheet"]
    assert all(r["blocks"] >= 5 and "[b1] " in r["prompt"] for r in doc["requests"])


# ------------------------------------------------------------------ quantize


DECODER = [f"model.language_model.layers.3.{m}" for m in (
    "input_layernorm", "self_attn.q_proj", "self_attn.k_proj", "self_attn.v_proj",
    "self_attn.o_proj", "post_attention_layernorm", "mlp.gate_proj", "mlp.up_proj",
    "mlp.down_proj")]  # fmt: skip
VISION = [
    f"model.visual.blocks.3.{m}"
    for m in (
        "norm1",
        "attn.qkv",
        "attn.proj",
        "norm2",
        "mlp.gate_proj",
        "mlp.up_proj",
        "mlp.down_proj",
    )
]


def _matches(pattern: str, name: str) -> bool:
    return re.fullmatch(pattern.removeprefix("re:"), name) is not None


def test_awq_mappings_cover_every_decoder_projection_and_never_the_vision_tower() -> None:
    patterns = [p for m in quantize.MAPPINGS for p in (m["smooth_layer"], *m["balance_layers"])]
    for name in DECODER:
        assert any(_matches(p, name) for p in patterns), name
    for name in VISION:  # llm-compressor's generic up_proj->down_proj would catch these
        assert not any(_matches(p, name) for p in patterns), name
    # and the vision tower + lm_head are not quantized at all
    assert any(_matches(p, "model.visual.blocks.0.mlp.up_proj") for p in quantize.IGNORE)
    assert "lm_head" in quantize.IGNORE


def test_the_recipe_is_int4_awq_with_group_128() -> None:
    spec = quantize.recipe_spec()
    assert spec["modifier"] == "AWQModifier" and spec["scheme"] == "W4A16_ASYM"
    assert spec["group_size"] == 128 and spec["targets"] == ["Linear"]
    assert spec["sequential_targets"] == ["Qwen2_5_VLDecoderLayer"]


def test_only_a_bf16_qwen2_5_vl_checkpoint_is_quantized() -> None:
    quantize.check_source({"architectures": ["Qwen2_5_VLForConditionalGeneration"]})
    with pytest.raises(ValueError, match="unsupported architecture"):
        quantize.check_source({"architectures": ["LlamaForCausalLM"]})
    with pytest.raises(ValueError, match="already quantized"):
        quantize.check_source(
            {
                "architectures": ["Qwen2_5_VLForConditionalGeneration"],
                "quantization_config": {"quant_method": "awq"},
            }
        )


def test_the_output_must_be_compressed_tensors_int4_that_vllm_detects() -> None:
    good = {
        "quantization_config": {
            "quant_method": "compressed-tensors",
            "config_groups": {
                "group_0": {"weights": {"num_bits": 4, "group_size": 128, "symmetric": False}}
            },
        }
    }
    quantize.check_output(good)
    with pytest.raises(ValueError, match="compressed-tensors"):
        quantize.check_output({})
    bad = json.loads(json.dumps(good))
    bad["quantization_config"]["config_groups"]["group_0"]["weights"]["num_bits"] = 8
    with pytest.raises(ValueError, match="int4"):
        quantize.check_output(bad)


def test_calibration_takes_every_doc_type_round_robin(tmp_path: Path) -> None:
    doc = corpus.read_corpus(_corpus(tmp_path, n=6))
    rows = quantize.calibration_rows(doc, 4)
    assert len(rows) == 4
    assert {r["doc_type"] for r in rows[:3]} == {"prescription", "lab_report", "vitals_sheet"}
    assert len(quantize.calibration_rows(doc, 128)) == 6  # a small corpus is used whole
    msg = quantize.calibration_messages(rows[0], "IMG")
    assert msg[0]["content"] == [{"type": "image", "image": "IMG"},
                                 {"type": "text", "text": rows[0]["prompt"]}]  # fmt: skip


def test_the_manifest_records_source_recipe_calibration_and_tools(tmp_path: Path) -> None:
    out = _corpus(tmp_path)
    ckpt = tmp_path / "ckpt"
    ckpt.mkdir()
    (ckpt / "config.json").write_text("{}", encoding="utf-8")
    rows = quantize.calibration_rows(corpus.read_corpus(out), 128)
    m = quantize.build_manifest(source=str(ckpt), corpus_dir=out, rows=rows, max_pixels=2_000_000,
                                versions={"llmcompressor": "0.7.1.4"}, seconds=12.34)  # fmt: skip
    assert m["block"] == "E2-S6" and m["method"] == "AWQ"
    assert m["source_config_sha256"] and m["source_index_sha256"] is None
    assert m["recipe"] == quantize.recipe_spec()
    assert m["calibration"]["corpus_digest"] == corpus.corpus_digest(out)
    assert m["calibration"]["samples"] == 3 and m["calibration"]["data_class"] == "synthetic"
    assert m["calibration"]["doc_types"] == {"lab_report": 1, "prescription": 1, "vitals_sheet": 1}
    assert m["tools"] == {"llmcompressor": "0.7.1.4"} and m["seconds"] == 12.3
    json.dumps(m)  # serialisable as written


def test_calibration_uses_the_serving_image_size() -> None:
    assert quantize.DEFAULT_MAX_PIXELS == Settings(_env_file=None).vlm_max_pixels_ocr  # type: ignore[call-arg]


# ------------------------------------------------------------------ the backend's new hooks


class UsageEngine:
    """vLLM-shaped fake: streams RequestOutputs with token ids; exposes ``vllm_config``."""

    def __init__(self, blocks: int = 1000, block_size: int = 16) -> None:
        self.vllm_config = SimpleNamespace(
            cache_config=SimpleNamespace(num_gpu_blocks=blocks, block_size=block_size)
        )
        self.in_flight = self.peak = 0
        self.lock = threading.Lock()

    async def generate(self, request: dict[str, Any], params: Any, request_id: str):
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.05)
            n_prompt = 100 + (300 if "multi_modal_data" in request else 0)
            from cdi_adapter.mlserve.backends import StubBackend

            text = (
                StubBackend().generate(request["prompt"], json_schema=params["schema"])
                if params.get("schema")
                else '{"confidence": 0.9}'
            )
            yield SimpleNamespace(
                prompt_token_ids=list(range(n_prompt)),
                outputs=[SimpleNamespace(text=text, token_ids=list(range(7)))],
            )
        finally:
            with self.lock:
                self.in_flight -= 1


def _vllm(engine: UsageEngine, settings: Settings | None = None,
          loaded: list[str] | None = None) -> VLLMBackend:  # fmt: skip
    def factory(mid: str, s: Settings) -> UsageEngine:
        if loaded is not None:
            loaded.append(mid)
        return engine

    return VLLMBackend(settings or _settings(), engine_factory=factory,
                       prompt_builder=lambda mid: (lambda p, img: p),
                       sampling=lambda n, g=None: {"max_tokens": n,
                                                   "schema": json.loads(g) if g else None},
                       grammar_builder=json.dumps)  # fmt: skip


def test_generate_with_usage_reports_prompt_and_generated_tokens() -> None:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="PNG")
    be = _vllm(UsageEngine())
    assert be.generate_with_usage("x", buf.getvalue()) == ('{"confidence": 0.9}', 400, 7)
    assert be.generate("x") == '{"confidence": 0.9}'  # the gateway contract is unchanged


def test_kv_cache_tokens_is_blocks_times_block_size() -> None:
    be = _vllm(UsageEngine(blocks=2048, block_size=16))
    assert be.kv_cache_tokens() == 32768 and be.loaded


# ------------------------------------------------------------------ bench: run


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            self.t += 0.5
            return self.t


def _gpu(name: str = "NVIDIA RTX PRO 4500 Blackwell", total: int = 32607, used: int = 29000):
    return lambda: {"name": name, "total_mib": total, "used_mib": used}


def test_run_variant_measures_the_production_backend(tmp_path: Path) -> None:
    engine, loaded = UsageEngine(blocks=1000, block_size=16), []
    out = _corpus(tmp_path, n=3)
    r = bench.run_variant("awq", "/models/cdi-dslm-awq", out, settings=_settings(),
                          levels=(1, 4), gpu=_gpu(), clock=Clock(),
                          backend_factory=lambda s: _vllm(engine, s, loaded))  # fmt: skip
    assert loaded == ["/models/cdi-dslm-awq"]
    assert r["variant"] == "awq" and r["kv_cache_tokens"] == 16000
    assert [s["id"] for s in r["serial"]] == ["doc0_pdf", "doc1_pdf", "doc2_pdf"]
    assert all(s["prompt_tokens"] == 400 and s["output_tokens"] == 7 for s in r["serial"])
    assert r["extract_s_per_doc"]["median"] == 0.5  # one clock tick per request
    assert r["tokens"]["footprint_p95"] == 407
    assert r["slots"] == 16000 // 407
    assert r["slots_worst"] == 16000 // (400 + 2600)  # the prescription budget, not the output
    k4 = r["concurrency"][1]
    assert k4["concurrency"] == 4 and k4["requests"] == 8 and engine.peak == 4
    assert r["corpus"]["digest"] == corpus.corpus_digest(out)
    assert r["engine"]["max_num_seqs"] == 4 and r["quantization"] is None
    json.dumps(r)


def test_a_variant_that_does_not_fit_fails_instead_of_measuring_the_3b(tmp_path: Path) -> None:
    class OutOfMemoryError(RuntimeError):
        pass

    tried: list[str] = []

    def factory(mid: str, s: Settings) -> UsageEngine:
        tried.append(mid)
        raise OutOfMemoryError("CUDA out of memory")

    def backend(s: Settings) -> VLLMBackend:
        return VLLMBackend(s, engine_factory=factory, prompt_builder=lambda m: (lambda p, i: p),
                           sampling=lambda n, g=None: {})  # fmt: skip

    with pytest.raises(OutOfMemoryError):
        bench.run_variant(
            "bf16",
            "Qwen/Qwen2.5-VL-7B-Instruct",
            _corpus(tmp_path),
            settings=_settings(),
            gpu=_gpu(),
            backend_factory=backend,
        )
    assert "Qwen/Qwen2.5-VL-3B-Instruct" not in tried  # no silent fallback


def test_the_quantization_manifest_travels_with_the_result(tmp_path: Path) -> None:
    model = tmp_path / "awq"
    model.mkdir()
    (model / bench.QUANT_MANIFEST).write_text(json.dumps({"source_model": "/m/dslm"}), "utf-8")
    r = bench.run_variant("awq", str(model), _corpus(tmp_path), settings=_settings(),
                          levels=(1,), gpu=_gpu(), clock=Clock(),
                          backend_factory=lambda s: _vllm(UsageEngine(), s))  # fmt: skip
    assert r["quantization"] == {"source_model": "/m/dslm"}


# ------------------------------------------------------------------ bench: compare


def test_percentile_and_slots() -> None:
    assert bench.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert bench.percentile([3.0], 50) == 3.0
    assert bench.kv_slots(32768, [1000, 2000, 4096]) == 8
    with pytest.raises(ValueError):
        bench.percentile([], 95)


def test_agreement_ignores_confidence_and_counts_exact_leaves() -> None:
    base = {"medications": [{"drug_name": "Tab A", "strength": {"value": 5}, "confidence": 0.9},
                            {"drug_name": "Tab B"}], "confidence": 0.9}  # fmt: skip
    same = json.loads(json.dumps(base))
    same["confidence"] = 0.4
    assert bench.agreement(base, same) == 1.0
    other = {"medications": [{"drug_name": "Tab A", "strength": {"value": 10}}]}
    assert bench.agreement(base, other) == pytest.approx(1 / 3)
    assert bench.agreement({"confidence": 1}, other) is None


def _result(variant: str, s_per_doc: float, texts: list[str], gpu: dict[str, Any],
            quant: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "variant": variant, "model": f"/m/{variant}", "quantization": quant,
        "corpus": {"digest": "a" * 64, "data_class": "synthetic", "requests": len(texts)},
        "engine": {"max_num_seqs": 4}, "gpu": gpu, "kv_cache_tokens": 20000,
        "slots": 6, "slots_worst": 3,
        "extract_s_per_doc": {"median": s_per_doc, "p95": s_per_doc * 1.2},
        "concurrency": [{"concurrency": 1, "docs_per_min": 5.0},
                        {"concurrency": 4, "docs_per_min": 14.0}],
        "serial": [{"id": f"d{i}", "schema": "vitals.v3", "text": t} for i, t in enumerate(texts)],
    }  # fmt: skip


VITALS_OK = json.dumps(
    {
        "vitals": [
            {
                "name": "heart_rate",
                "value": "78",
                "unit": "/min",
                "evidence": ["b4"],
                "confidence": 0.9,
            }
        ],
        "confidence": 0.9,
    }
)


def test_compare_gives_speedup_validity_agreement_and_the_sizing_tag() -> None:
    blackwell = {"name": "NVIDIA RTX PRO 4500 Blackwell", "total_mib": 32607, "used_mib": 29000}
    l4 = {"name": "NVIDIA L4", "total_mib": 23034, "used_mib": 20000}
    drifted = VITALS_OK.replace('"78"', '"87"')
    s = bench.compare([
        _result("baseline-bf16", 12.0, [VITALS_OK, VITALS_OK], blackwell),
        _result("dslm-awq", 4.0, [VITALS_OK, drifted], blackwell, {"source_model": "/m/dslm"}),
    ])  # fmt: skip
    base, awq = s["rows"]
    l4row = bench.compare(
        [
            _result("baseline-l4", 12.0, [VITALS_OK, VITALS_OK], l4),
            _result("awq-on-l4", 6.0, [VITALS_OK, "not json"], l4, {"source_model": "/m/dslm"}),
        ]
    )["rows"][1]
    assert awq["concurrent_slots"] == 4 and awq["kv_slot_estimate"] == 6
    assert base["speedup_vs_baseline"] == 1.0 and base["quantization"] == "bf16"
    assert awq["speedup_vs_baseline"] == 3.0 and awq["quantization"] == "AWQ W4A16"
    assert awq["source_model"] == "/m/dslm" and awq["docs_per_min"] == 14.0
    assert awq["at_concurrency"] == 4 and awq["vram_used_gib"] == 28.3
    assert awq["schema_valid"] == "2/2" and awq["agreement_with_baseline"] == pytest.approx(0.875)
    assert awq["tag"] == "MEASURED"
    assert l4row["schema_valid"] == "1/2"
    assert l4row["tag"] == "MEASURED on NVIDIA L4 (not the 32 GB target)"  # never passed off


def test_compare_needs_a_variant_besides_the_baseline() -> None:
    with pytest.raises(ValueError):
        bench.compare([_result("b", 1.0, [VITALS_OK], _gpu()())])


def test_compare_cli_archives_json_and_markdown(tmp_path: Path, capsys) -> None:
    g = _gpu()()
    paths = []
    for name, sec in (("baseline-bf16", 10.0), ("dslm-awq", 5.0)):
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(_result(name, sec, [VITALS_OK], g)), encoding="utf-8")
        paths.append(str(p))
    assert bench.main(["compare", *paths, "--archive", str(tmp_path / "arch")]) == 0
    files = sorted((tmp_path / "arch").iterdir())
    assert [f.suffix for f in files] == [".json", ".md"]
    assert re.fullmatch(r"\d{8}T\d{6}Z-nvidia-rtx-pro-4500-blackwell\.md", files[1].name)
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert len(saved["results"]) == 2 and saved["summary"]["rows"][1]["speedup_vs_baseline"] == 2
    md = files[1].read_text(encoding="utf-8")
    assert "| dslm-awq | bf16 |" in md and "| MEASURED |" in md
    assert "E2-S6 benchmark" in capsys.readouterr().out


# ------------------------------------------------------------------ the quantization venv


def test_the_quant_venv_is_locked_with_the_compressed_tensors_vllm_reads() -> None:
    from cdi_adapter.podmanifest import TORCH_FAMILY, parse_lock, parse_versions

    pins = parse_versions((RUNPOD / "versions.env").read_text(encoding="utf-8"))
    text = (RUNPOD / "requirements-quant.lock").read_text(encoding="utf-8")
    lock = parse_lock(text)
    vllm_lock = parse_lock((RUNPOD / "requirements-vllm.lock").read_text(encoding="utf-8"))
    assert lock["llmcompressor"] == pins["LLMCOMPRESSOR_VERSION"] == "0.7.1.4"
    assert lock["transformers"] == pins["QUANT_TRANSFORMERS_VERSION"]
    # the checkpoint format the quantizer writes is the one the gateway reads
    assert lock["compressed-tensors"] == vllm_lock["compressed-tensors"] == "0.11.0"
    assert {"qwen-vl-utils", "datasets", "accelerate"} <= lock.keys()
    assert not [n for n in lock if TORCH_FAMILY.match(n)]  # torch 2.8 from the image / cu128
    assert "torch==2.8.0" in text.split("\n\n", 1)[0]
    blocks = text.split("\n\n", 1)[1].strip().split("\n")
    assert sum(ln.startswith("    --hash=sha256:") for ln in blocks) >= len(lock)


def test_the_run_script_wires_the_three_venvs_in_order() -> None:
    sh = (RUNPOD / "dslm_quantize_bench.sh").read_text(encoding="utf-8")
    assert '--require-hashes --no-deps -r "$2"' in sh
    assert 'venv "$REPO/.venv-quant" "$HERE/requirements-quant.lock"' in sh
    order = [sh.index(s) for s in (
        ".venv/bin/python\" -m cdi_adapter.dslm.corpus build",
        ".venv-quant/bin/python\" -m cdi_adapter.dslm.quantize",
        ".venv-vllm/bin/python\" -m cdi_adapter.dslm.bench run",
        ".venv/bin/python\" -m cdi_adapter.dslm.bench compare")]  # fmt: skip
    assert order == sorted(order)
    assert "DATA_CLASS=${DATA_CLASS:-synthetic}" in sh
