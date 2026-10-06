# De-identification before training / evaluation use (backlog E11-S4)

**Rule:** no real patient document enters the E2-S4 gold set, E2-S5 fine-tuning, or any other
training or evaluation corpus until it has been through this pipeline. It runs **on the pod**,
where the data is. No text or image leaves the host during de-identification: the models load
from local files only.

## What it does

| Step | How |
|---|---|
| Find identifiers | **Presidio** with spaCy `en_core_web_lg`, plus Indian recognizers (below), plus the clinical de-identification NER **`obi/deid_roberta_i2b2`** (MIT) pinned at commit `78f2152e`. Recall comes first; the threshold is `CDI_DEID_MIN_SCORE` (0.35) |
| Replace in text and answer keys | Names become consistent **surrogate names** (the same person keeps the same fake name within one document, and it differs across documents). **Dates** shift by a random per-document offset (30 to 365 days), keeping their format and the gaps between them. Every ID becomes a **typed placeholder** (`[ABHA]`, `[AADHAAR]`, `[PHONE]`, `[ID]` …). Ages over 89 become `90+`. JSON fields named `name`, `dob`, `abha*`, `phone`, `address` … are replaced even if the detector misses them |
| Black out on page images | Every OCR line holding an identifier is covered, whole line plus a 4 px margin. The redacted image is then **read again**, and anything still identifying is blacked out too (up to 3 passes), because OCR misses and splits lines |

**Indian identifiers covered:**
- ABHA number and ABHA address
- Aadhaar (Verhoeff check digit, so random 12-digit numbers are not flagged)
- PAN
- phone numbers, including OCR-glued ones like `Mobile:+919105446581E`
- PIN codes (only when labelled)
- the value after `MRN` / `UHID` / `IP No` / `Reg No` … (the label itself is kept)
- CareFlow MPI ids and legacy MRNs
- the whole value after `Address:`
- e-mail
- dates in Indian forms

**Kept on purpose, because the training data needs it:**
- form labels (`DOB:`, `ABHA No:` …)
- durations (`for 3 days`)
- prescription abbreviations (`SOS`, `OD`, `BD` …)
- lab values and reference ranges (`88000 /uL 150000-410000`)
- ages up to 89

**Nothing reversible is stored:**
- The per-document key (surrogate salt and date shift) lives only in memory.
- The manifest records entity counts and model versions, never a removed value.
- Output files are renamed (`deid_00000.png` …), because an original file name may itself
  identify the patient.

## Setup on the pod (once)

The `deid` extra and the spaCy model wheel are in `infra/runpod/requirements.lock`, so every boot
installs them hash-checked (E1-S4). The NER model (about 1.4 GB) is fetched once, at its pinned
commit:

```bash
cd /workspace/cdi && HF_HOME=/workspace/hf-cache .venv/bin/python -m cdi_adapter.deid fetch-models
```

## Use

```bash
# scanned pages (+ optional same-named .json answer key) -> de-identified copies
HF_HOME=/workspace/hf-cache HF_HUB_OFFLINE=1 .venv/bin/python -m cdi_adapter.deid scans IN_DIR OUT_DIR
# a quick look at one text
.venv/bin/python -m cdi_adapter.deid text "Name: ... DOB: ..."
```

`OUT_DIR` gets, per page:
- `deid_NNNNN.png`: the redacted image
- `.txt`: the de-identified OCR text
- `.json`: the de-identified answer key
- plus `manifest.json`

A person must still review a sample before a corpus is released: that's the E2-S4 🔒
"de-identification approval". This pipeline is what makes the review small, not a replacement for
it.

## Red-team check (the E11-S4 acceptance test)

```bash
HF_HUB_OFFLINE=1 python scripts/deid_redteam.py --patients 12 --seed 47 --report redteam.json
```

The sample is synthetic patients carrying every identifier kind above, printed as scanned
registration forms, OPD notes and lab reports with answer keys. The attacker knows the true
identifiers and searches the output in three places:
- the de-identified text
- the answer key
- the redacted image, **re-read by OCR with no confidence cut-off**

It looks for digits of IDs (also the last 6 alone), name tokens (also with one OCR-style typo),
the date of birth in its written forms, and addresses and e-mails. The **control** runs the same
attack on the original and must find the identifiers. The check passes only when every control
succeeds and nothing leaks.

Each new failure mode it found became a unit test in `tests/test_deid_unit.py`. They include:
- OCR gluing words (`1987Age`)
- a split Aadhaar (`48031611.6055`)
- a date swallowed by an "age" hit
- lines the OCR dropped as low-confidence

`tests/test_deid_models.py` runs the full stack when `CDI_TEST_DEID_MODELS=1`.

## Local development (Windows)

Smart App Control blocked two compiled libraries in their newest releases, so the dev venv uses
`regex 2025.10.22` and `spaCy 3.8.7`, which it allows. The pod (Linux) uses the locked versions.
