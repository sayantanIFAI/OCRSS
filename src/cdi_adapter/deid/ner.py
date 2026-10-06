"""Clinical de-identification NER as a Presidio recognizer (backlog E11-S4; ARCHITECTURE §12).

``obi/deid_roberta_i2b2`` (MIT; RoBERTa fine-tuned on the i2b2 2014 de-identification corpus)
tags PHI in clinical text with BILOU labels: PATIENT, STAFF, AGE, DATE, PHONE, ID, EMAIL, PATORG,
HOSP, LOC, OTHERPHI. It is loaded from the local Hugging Face cache at a pinned revision only
(``local_files_only``): nothing is downloaded while de-identifying, and no text leaves the host.
``python -m cdi_adapter.deid fetch-models`` puts the pinned files in the cache once.

Long OCR text is run in overlapping windows (the model sees at most 512 tokens).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from presidio_analyzer import AnalysisExplanation, EntityRecognizer, RecognizerResult

# obi tag -> the entity names the rest of the pipeline uses
LABELS = {
    "PATIENT": "PERSON", "STAFF": "PERSON", "AGE": "AGE", "DATE": "DATE_TIME",
    "PHONE": "PHONE_NUMBER", "ID": "MEDICAL_RECORD_NUMBER", "EMAIL": "EMAIL_ADDRESS",
    "PATORG": "ORGANIZATION", "HOSP": "ORGANIZATION", "LOC": "LOCATION", "OTHERPHI": "OTHER_PHI",
}  # fmt: skip
WINDOW, STRIDE = 510, 384  # tokens per window, step between windows (overlap 126)
NAME = "clinical_deid_ner"  # engine.clinical_false_positive keys on it


@lru_cache(maxsize=2)
def _load(model_id: str, revision: str) -> tuple[Any, Any]:
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_id, revision=revision, local_files_only=True)
    model = AutoModelForTokenClassification.from_pretrained(
        model_id, revision=revision, local_files_only=True
    )
    model.eval()
    return tok, model


def tag_spans(text: str, model_id: str, revision: str) -> list[tuple[str, int, int, float]]:
    """(obi type, start, end, score) character spans, windows merged, BILOU collapsed."""
    import torch

    tok, model = _load(model_id, revision)
    enc = tok(text, return_offsets_mapping=True, add_special_tokens=False)
    ids, offsets = enc["input_ids"], enc["offset_mapping"]
    id2label = model.config.id2label
    best: dict[int, tuple[str, float]] = {}  # token index -> (label, prob), best over windows
    start = 0
    while True:
        chunk = ids[start : start + WINDOW]
        inputs = torch.tensor([[tok.cls_token_id, *chunk, tok.sep_token_id]])
        with torch.no_grad():
            probs = model(input_ids=inputs).logits[0, 1:-1].softmax(-1)
        conf, pred = probs.max(-1)
        for k in range(len(chunk)):
            label, p = id2label[int(pred[k])], float(conf[k])
            if label != "O" and p > best.get(start + k, ("", 0.0))[1]:
                best[start + k] = (label, p)
        if start + WINDOW >= len(ids):
            break
        start += STRIDE

    spans: list[tuple[str, int, int, float]] = []
    for k in sorted(best):
        label, p = best[k]
        kind = label.split("-", 1)[-1]
        s, e = offsets[k]
        if s == e:
            continue
        if spans and spans[-1][0] == kind and text[spans[-1][2] : s].strip() == "":
            prev = spans[-1]
            spans[-1] = (kind, prev[1], e, min(prev[3], p))  # join adjacent pieces of one entity
        else:
            spans.append((kind, s, e, p))
    return spans


class ClinicalNerRecognizer(EntityRecognizer):
    def __init__(self, model_id: str, revision: str) -> None:
        self.model_id, self.revision = model_id, revision
        super().__init__(supported_entities=sorted(set(LABELS.values())),
                         name=NAME, supported_language="en")  # fmt: skip

    def load(self) -> None:  # the model is loaded on first use (and cached)
        pass

    def analyze(self, text: str, entities: list[str], nlp_artifacts: Any = None
                ) -> list[RecognizerResult]:  # fmt: skip
        out = []
        for kind, start, end, score in tag_spans(text, self.model_id, self.revision):
            entity = LABELS.get(kind, "OTHER_PHI")
            if entities and entity not in entities:
                continue
            why = AnalysisExplanation(recognizer=NAME, original_score=score,
                                      textual_explanation=f"{self.model_id}: {kind}")  # fmt: skip
            out.append(RecognizerResult(entity, start, end, score, analysis_explanation=why))
        return out
