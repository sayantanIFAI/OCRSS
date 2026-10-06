# Medicine catalogue (Tata 1mg) — identification layer

Owner request (2026-09-26): use the Tata 1mg medicine catalogue as the master list that extracted
medicine names are identified against. The layer runs **after** extraction and S5 terminology and
never replaces them: the model's text (`clinical_fact.local_text`, `medication_detail.drug_text`)
stays exactly as read; the catalogue's answer is stored beside it.

```
S4 extract (VLM)  ->  S5 terminology (SNOMED seed)  ->  catalogue identification  ->  S6 validate
   "Tab. zincolit"        local_only                      Zincovit Tablet (0.88, fuzzy)
   "Tab. PCM - 650"       local_only                      UNKNOWN_MEDICATION (+ closest products)
```

## Source and format

| Option | Use | Notes |
|---|---|---|
| **Tata 1mg partner data** (catalogue export or API under a partnership) | **Production** | The only source with clear rights to use the data. 1mg offers partner/API integrations ([partnerships](https://www.1mg.com/partnerships), [merchant API docs](https://onedoc.1mg.com/public_docs/content/Merchant%20Integration%20Documentation/Phamacy%20API%20Integration/api-integration/)); catalogue licensing has to be agreed with them. |
| Community snapshot "A-Z Medicine Dataset of India" (Kaggle, ~250k allopathy products, scraped from 1mg, prices as of Nov 2022) | Development / evaluation only, after the owner accepts its terms | Same fields as a 1mg product page; it is **not** published by Tata 1mg, so its licence must be checked before any production use. |
| Scraping 1mg.com ourselves | **Not used** | Not ours to take; the pipeline never fetches catalogue data from the web. |

The importer reads a **CSV snapshot** (one product per row); nothing is called at run time, which
also keeps extracted names inside our hosts. Header names are matched case-insensitively; the first
present wins (`catalogue/service.py: COLUMNS`):

| Field | Accepted headers |
|---|---|
| product id | `id`, `sku_id`, `product_id`, `sku` |
| **name** (required) | `name`, `product_name`, `medicine_name`, `sku_name` |
| manufacturer | `manufacturer_name`, `manufacturer`, `marketer`, `company` |
| dosage form / type | `type`, `dosage_form`, `form`, `product_form` |
| pack | `pack_size_label`, `pack_size`, `packaging`, `pack` |
| composition | `composition`, `salt_composition`, `short_composition1` (+ `short_composition2`) |
| price | `price(₹)`, `price`, `mrp` |
| discontinued | `is_discontinued`, `discontinued` |

```bash
python -m cdi_adapter.catalogue import --file tata1mg_catalogue.csv   # load / reload (replaces the source's rows)
python -m cdi_adapter.catalogue stats                                 # products + the latest import (file, sha256)
python -m cdi_adapter.catalogue identify "Tab. Montina-FX"            # try one name
```

Every load is recorded in `med_catalogue_import` (file name, **sha256**, rows, who) and each match
row keeps the import it was made against.

## How a name is identified

1. **Normalise** (`catalogue/normalize.py`): lower-case, drop dosage-form words (Tab, Cap, Syp,
   Tablet, …) and units, split out strengths: `"Tab. Dolo 650"` → brand `dolo`, strength `650`.
   A release form written before the dosage form stays in the brand ("Glycomet SR Tablet"); words
   after the dosage form qualify the form, not the brand ("Sompraz D 40 Capsule SR" → `sompraz d`;
   ~18 000 names in the 1mg layout are written this way). Brand keys are computed at import, so a
   change to the normaliser needs the catalogue imported again.
   How prescriptions are written is read through, on the query side only: a form fused to the
   name ("CapUprise" → Cap Uprise), a thousands separator ("60,000U"), "60K" = 60 000. A leading
   prescription marker ("Ad. Sompraz", "Rx Dolo") is dropped only if the name as written finds
   nothing (some brands begin with "AD"). A key with fewer than 3 letters ("T", "CAP") is never
   identified.
2. **Generic name** (the key is an ingredient of some product's composition: Amlodipine,
   Metformin) → matched as that generic (`method=generic`, e.g. "Amlodipine (generic)", no
   product id): a line written by salt name is not one maker's product, and never a look-alike
   such as "S Amlodipine Tablet". A generic in brackets beside an unknown brand
   ("Nofza (Tofacitinib)") counts only when it is exactly a catalogue generic.
   **Exact brand** hit → matched (`method=exact`, score 1.0).
3. Otherwise **candidates** from PostgreSQL `pg_trgm` (GIN trigram index on `brand_norm`), re-scored
   with an OCR-aware edit similarity: glyphs readers confuse are folded first (`rn`↔`m`, `0`↔`o`,
   `1`↔`l`, …); one wrong letter in an eight-letter brand scores 0.875. A different number in
   the name is a different product and scores 0 (Uprise D2 / Uprise-D3). Misread generics
   ("Metfornin") compete under the same rules (`method=generic_fuzzy`). The **dosage form written
   on the line** (Tab. / Cap. / Syp. / Inj., taken from the fact's text even when the extracted
   drug name dropped it) must agree with a fuzzy match: "Cap. Sonpraz" is not the Sompraz tablet
   (the prescription said Cap. Sompraz D) — it is UNKNOWN, with capsules listed first among the
   closest products. For an exact brand the form only picks the pack.
4. **Accepted only if** the best brand scores ≥ `CDI_CATALOGUE_MIN_SCORE` (0.85) **and** leads every
   other brand by ≥ `CDI_CATALOGUE_MIN_MARGIN` (0.05). Brands shorter than 5 letters must match
   exactly (Dolo / Dola: one misread letter is another medicine). A fuzzy name whose strength
   contradicts the catalogue is not accepted (`CDI_CATALOGUE_STRENGTH_MUST_AGREE`).
5. The pack: the brand's product with exactly the written strengths (Glycomet-GP 1, not 1/850),
   then one sharing a strength, then one still on the market.
6. Anything else → **`UNKNOWN_MEDICATION`**, with the closest products kept as `candidates` for the
   reviewer. Never a guess. The closest list is for a person to choose from, so it uses every
   clue the line has: the nearest names (short ones too: "Shaz" / "Saaz"), then **products of the
   written strength** even when the name is far ("Muronorm 62.5" → Thyronorm 62.5mcg Tablet
   first, not the Meronorm injection), nudged by the written dosage form. Strength and form
   only nudge the order, because they can be misread too. A product whose name states no
   strength takes its composition's ("Saaz Tablet" = Sulfasalazine 500 mg); "#5" is a quantity.

Measured (local PostgreSQL 16, 250 000 synthetic products): import 18.5 s; identification median
71 ms, p95 115 ms. Embeddings were not used: brand names are proper nouns whose errors are
character-level misreads, which edit distance models directly; a semantic embedding would add a
model to run and still confuse look-alike brands.

## Where it shows

* **Reviewer** → Medications: column *Catalogue (Tata 1mg)* — product · manufacturer · score, or
  `UNKNOWN_MEDICATION (closest: …)`. The *Medicine* column stays as written.
* **Reviewer** → Medications → **Replace** (2026-09-28): lists the products offered for the line
  *From the catalogue* (its match, closest products, catalogue shortlist) and *From the medication
  agent* (its product and the options it chose from), plus a catalogue search by brand or
  generic. The reviewer picks one; it becomes the line's medicine through the same correction as
  Edit (dose, frequency, duration, route and instructions are kept), and the line's catalogue
  identification becomes that product with method `reviewer` ("chosen by reviewer"). A rerun of
  the agent treats such a line as settled. API: `GET /api/reviewer/facts/{id}/replacements?q=`,
  `POST /api/reviewer/facts/{id}/replace` `{catalogue_id | generic, source, note}`
  (`webapp/med_replace.py`); the item's history gets a `correct` action with
  `replace_medication: true`. Strength is not changed automatically: a pack whose strength
  differs from the line is flagged, and the reviewer corrects the strength with Edit.
* **Pipeline Monitor** → Terminology step: the same per line; the step's metrics carry
  `catalogue_matched` / `catalogue_unknown`.
* Tables: `med_catalogue`, `med_catalogue_import`, `medication_catalogue_match` (migration 0009).
* Not in the FHIR bundle: a 1mg product id is not a code system the ABDM IG recognises; the
  medication's SNOMED coding is unchanged.

`CDI_CATALOGUE_ENABLED=false`, or an empty catalogue, turns the layer off without changing
anything else.
