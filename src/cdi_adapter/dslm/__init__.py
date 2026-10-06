"""Fine-tuned DSLM tooling (ARCHITECTURE §14 #1d): quantization and serving benchmarks.

Backlog E2-S6, runbook ``docs/quantization.md``. Three steps, each in the venv it needs:

* ``corpus``   (pipeline venv) - the real S4 extraction requests for a set of documents
* ``quantize`` (``.venv-quant``) - AWQ/INT4 of a checkpoint, calibrated on that corpus
* ``bench``    (``.venv-vllm``) - replays the corpus through the production vLLM backend

The modules import only the standard library at import time, so each venv can load the shared
corpus reader without the others' dependencies.
"""
