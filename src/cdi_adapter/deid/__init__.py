"""De-identification before any training / evaluation use (backlog E11-S4; ARCHITECTURE §12).

Presidio + spaCy + a clinical de-identification NER find direct identifiers; they are replaced in
text and JSON and blacked out on page images; a red-team check proves a sample cannot be
re-identified. Runbook: docs/deid.md. Needs the ``deid`` extra and the pinned models.
"""
