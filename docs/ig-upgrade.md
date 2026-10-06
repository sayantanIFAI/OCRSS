# ABDM IG: pinned validator, golden bundles, and the upgrade procedure (E6-S1, E6-S4, E6-S5)

## What is pinned (E6-S1)

| Pin | Where | Value today |
|---|---|---|
| ABDM IG, by its FHIR registry id | `CDI_IG_PACKAGE` (`.env`), `FHIR_IG` + `FHIR_IG_SHA256` (`infra/runpod/versions.env`) | `ndhm.in#6.5.0`, sha256 `fc5a7a53…` |
| Every package the validator loads alongside the IG | `infra/fhir/packages.lock` (`id#version` + content digest) | 10 packages: FHIR core 4.0.1, THO terminology, extensions |
| HL7 validator | `FHIR_VALIDATOR_VERSION` + `FHIR_VALIDATOR_SHA256` | `validator_cli.jar` 6.10.4 |
| Java runtime | `APT_PINS` (E1-S4 snapshot) | `openjdk-21-jre-headless=21.0.12+8-1~24.04` |

**The IG id.** The NRCeS ABDM IG is published in the FHIR package registry as **`ndhm.in`**, with
canonical `https://nrces.in/ndhm/fhir/r4`. That canonical is what our `meta.profile` URLs use.
`nrces.fhir.r4.ndhm`, the value used before E6-S1, is not a registry id, so the validator could
never have loaded it. The old code also dropped the version (`split("#")[0]`), so even a correct
id would have loaded whatever version was newest. Both are fixed: `validate_abdm.hapi_command`
passes the pinned tarball, `<CDI_FHIR_PACKAGE_HOME>/ig/ndhm.in-6.5.0.tgz`.

**Offline, always.** Validation runs with `-tx n/a -no-http-access`, and a Java proxy on a closed
port as a second fence. There is no terminology server and no package download in the hot path
(§12). The validator's package cache is `CDI_FHIR_PACKAGE_HOME/.fhir/packages`, reached via
`-Duser.home`.

**Every boot** (`bootstrap_pod.sh` step 5b):
1. Refuse if `CDI_IG_PACKAGE` ≠ `FHIR_IG`.
2. Fetch the jar if its sha256 differs, and verify it.
3. Run `python scripts/fhir_ig.py seed --home $CDI_FHIR_PACKAGE_HOME`. This installs the IG
   tarball (sha256-checked) and exactly the locked `id#version` list from the registry, then
   proves every digest. Any difference prints `DRIFT` and the boot stops. The seed never
   resolves "latest".

## Golden bundles (E6-S4)

`tests/golden/fhir/<Artifact>.json` holds one document bundle for each of PrescriptionRecord,
DiagnosticReportRecord, OPConsultRecord, DischargeSummaryRecord and WellnessRecord. They are built
from fixed synthetic inputs through the real S9 assembly (`fhir.service.assemble_document_bundle`).

```bash
python scripts/fhir_golden.py --check        # CI: a builder change that alters a golden fails
python scripts/fhir_golden.py --write        # accept a deliberate builder change (review the diff)
python scripts/fhir_golden.py --validate     # the real validator: no error beyond the baseline
```

**The honest state.** Under the real validator the goldens are **not yet IG-conformant: 120
errors across the five**, recorded in `tests/golden/fhir/validation-baseline.json`. The main
causes:
- `Patient.identifier.type` is missing (E6-S6).
- The Encounter profile does not match.
- There are no HFR/HPR identifiers on Organization/Practitioner (E8-S1 🔒).
- Composition sections don't follow the ABDM section slices (E6-S7).
- Smaller gaps: `Bundle.meta.versionId`, the ICD-10 display, BP sent as a string, the SpO2 LOINC
  2708-6 code, and DiagnosticReport interpreter/conclusion.

The Python fallback (`python_checks`) never saw any of these. The baseline is a **ratchet**:
`--validate` fails on any error not in it, and reports fixed ones so it can shrink
(`--validate --write-baseline`). The target is an empty baseline.

## Upgrading the IG (E6-S5): staging, then E6-S4, then production

Never edit the pins by hand. On a staging host (or the pod, in a separate folder):

```bash
# 1. stage the candidate in its OWN package home (refused if it is the production home)
python scripts/fhir_ig.py stage --ig ndhm.in#6.6.0 --home /workspace/tools/fhir-staging \
    --java java --jar /workspace/tools/validator_cli.jar
```

`stage` does the following:
1. Fetches the candidate tarball.
2. Lets the validator, online this once, install whatever that IG version depends on into the
   staging cache.
3. Re-runs the E6-S4 goldens **offline**, exactly as production would.
4. Writes `stage-report.json`.

It **passes** only if the goldens still match the builders **and** no golden gains an error
beyond the current baseline. Errors the new IG fixes are listed.

```bash
# 2. see exactly what would change; nothing is written
python scripts/fhir_ig.py promote --ig ndhm.in#6.6.0 --home /workspace/tools/fhir-staging --dry-run
# 3. flip the pins (refused unless step 1 passed for this exact IG and the staging cache is unchanged)
python scripts/fhir_ig.py promote --ig ndhm.in#6.6.0 --home /workspace/tools/fhir-staging
```

`promote` rewrites these together:
- `infra/fhir/packages.lock`
- `FHIR_IG` / `FHIR_IG_SHA256` in `versions.env`
- `CDI_IG_PACKAGE` in `.env.runpod` and `.env.example`

Then, by hand:
1. Update the `ig_package` default in `config.py` and the ARCHITECTURE §9 row (a test fails until
   they agree).
2. If the new IG fixed errors, run `fhir_golden.py --validate --write-baseline`.
3. Run `scripts/ci.py`.
4. Deploy, and reboot the pod.

**Production changes only then.** Step 5b of the boot installs and proves the new set, and refuses
if `.env` and the pins disagree.

**Rollback** means promoting the previous version the same way: stage it, then promote it.

**Upgrading the validator jar** follows the same path:
1. Update `FHIR_VALIDATOR_VERSION` and `FHIR_VALIDATOR_SHA256`.
2. Stage the *current* IG with the new jar.
3. Promote it: the package set the new jar wants lands in the lock.
