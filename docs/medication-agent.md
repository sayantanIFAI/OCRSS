# Medication agent — diagnosis-aware medicine identification

Owner request (2026-09-26): *"utilise the agent where it checks the diagnosis along with extracted
data and compare the catalogue, then matches the appropriate diagnosis with the extracted data"*.
Backlog **E12-S4** (reviewer copilot, for medication lines) and **E12-S5** (agent trace), inside the
bounds of [ADR-0003](adr/0003-agentic-control-plane.md).

## Why

The catalogue layer ([medicine-catalogue.md](medicine-catalogue.md)) matches a medicine by its
spelling. A badly misread handwritten name is too far from any product to match safely, so it
stays `UNKNOWN_MEDICATION`. Example (owner's test prescription):

| On the paper | Model read | Diagnosis on the Rx | Agent's route to the product |
|---|---|---|---|
| Tab. Thyronorm 62.5 mcg | Muronorm | Hypothyroid | thyroxine 62.5 → Thyronorm 62.5mcg Tablet |
| Tab. Saaz 500 mg | Shaz | Rheumatoid arthritis | sulfasalazine 500 → Saaz Tablet |
| Tab. Tfct-Nib 5 mg | FESNIB | Rheumatoid arthritis | tofacitinib 5 → Tfct-Nib Tablet |
| Cap. Uprise-D3 60K | Lipasite-D3 | GC-induced osteoporosis | vitamin D3 60 000 IU → Uprise-D3 60K Capsule |

## How (per page with medicine lines, after the document is filed for review)

0. **Recall** (owner request 2026-09-28; `CDI_AGENT_RECALL`, default on) — the model (text only)
   gets each line the catalogue did not identify exactly **as extracted** (the written text plus
   the strength, form, dose, frequency, duration, route and instructions read from it) and the
   prescription's **diagnoses**, and names from its own knowledge of Indian medicines the brands
   and generics the line most likely is, with a one-line reason (schema `med_agent_recall.v1`).
   Its answer only **widens** step 1's shortlist (the named brands and the products of the named
   generics); the product is still chosen against the page (step 2) and guarded (step 3). The
   choose question then also carries the extracted line and the diagnoses. The suggestion records
   `recalled` and `knowledge_options` (the shortlisted products the recall pointed to); the
   Reviewer's **Replace** lists those first, labelled "from its knowledge and the diagnoses", and
   the Agent column shows "agent thinks: …" for a line without a confident match.
   `CDI_AGENT_RECALL=false` runs the agent exactly as before this step.
1. **Shortlist** — catalogue tool, no model (`catalogue.service.shortlist`): for each line the
   catalogue did not identify exactly, the products of the written strength and form whose names
   are nearest the reading, plus the closest names; one per brand, at most `CDI_AGENT_MAX_OPTIONS`.
2. **Choose** — the VLM, with the page image, picks the option actually written on each line, or 0
   (schema `med_agent_choice.v1`).
3. **Guard** (ADR-0003 rule 4, veto only) — a choice outside the shortlist is dropped; a product
   whose name does not look like the line's own reading (`nearness < CDI_AGENT_MIN_GROUNDING`) is
   **vetoed**. The model's quoted "evidence" counts only when it is its own reading, not the
   option's name copied back.
4. **Fit** — the model (text only), given what each identified product *contains*, names the ONE
   diagnosis on the prescription it treats, or none (schema `med_agent_fit.v1`); an answer that is
   not one of the prescription's diagnoses is dropped.
5. **Verify** — a yes/no second opinion on every medicine-diagnosis link (schema
   `med_agent_verify.v1`); an unconfirmed link is dropped and the line shows **"no matching
   diagnosis"**.

Lines the catalogue already identified exactly skip steps 1-3 and get steps 4-5 only.

Measured on the owner's test prescription (Qwen2.5-VL-7B, 2026-09-26): the choice was right for
Saaz, Thyronorm, Lonazep and Uprise-D3, and wrong for Tfct-Nib (it chose Tosanib, the same salt:
Tfct-Nib was not in the name-based shortlist). Asked "which diagnosis?", the 7B linked clonazepam
to asthma; asked the pair yes/no it said no, hence step 5. It did not link vitamin D3 to
"GC Induced OP" (errs to "no matching diagnosis"). Earlier designs that had the model name the
generics from the misread name, or list the drugs per diagnosis, were measured and dropped:
the 7B guessed from the misspelling and its per-diagnosis lists were too short. The recall step
(step 0) differs from those: it sees the whole extracted line and the diagnoses together, and its
answer can only add catalogue options, never become the suggestion without the page choice and
the guard. It has not yet been measured on the owner's test prescriptions with the real 7B.

The model gateway normally tells the model to copy values only from the page; the agent's
schemas (`cdi:med_agent_*`) get a separate instruction, because steps 4-5 ask for medical
knowledge.

## What it may and may not do

* **Advisory only** (ADR-0003 rule 3): it writes an `ai_event` (kind `medication_agent`) with one
  `ai_suggestion` per medication fact. It never changes a fact, a review state or the FHIR bundle.
* The Reviewer shows **Agent (diagnosis check)** per medicine: the suggested product with its
  confidence, the diagnosis it treats or **"⚠ no matching diagnosis"**, the text as written and the
  rationale. **Use suggestion** opens the normal Edit form pre-filled; saving it is the reviewer's
  correction, after which the catalogue identifies the new name exactly.
* **Run agent again** re-queues it (e.g. after a diagnosis was corrected); the newest run wins.
* Every step (model calls, catalogue calls, guard decisions) is in `agent_trace` with its input,
  output, confidence and timing; the Pipeline Monitor shows the run as the **Med agent** node.

## When it runs

Automatically, as Celery task `cdi.medication_agent` on the `cdi-web` queue, at the end of the
Generate EMR run: once the web job has validated a document and filed it for review, so the upload reaches review without waiting for it (about a minute of
GPU per prescription with `hf` (about 15 s measured); the gateway runs one forward pass at a time). Off with
`CDI_AGENT_ENABLED=false`, and a no-op without a loaded catalogue.

```bash
python -m cdi_adapter.agent run <document id>     # run it by hand
```
