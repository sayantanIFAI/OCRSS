"""Typed settings for every CDI-Adapter process (ARCHITECTURE §9).

Every value is overridable per environment with the ``CDI_`` prefix (env var or ``.env``).
The code default for the model backend is ``stub`` so tests and CI need no GPU; the pod sets
``CDI_MLSERVE_BACKEND=hf`` in its ``.env``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CDI_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # adapter DB / broker. Credentials (these four) come from the secrets store in any shared
    # deployment (backlog E1-S5); the defaults are local-dev only and `secretstore check` rejects
    # them. repr=False keeps them out of logs and tracebacks.
    database_url: str = Field(default="postgresql+psycopg://cdi:cdi@127.0.0.1:5432/cdi", repr=False)
    # E10-S6: the primary and its standby, e.g. "127.0.0.1:5432,127.0.0.1:5433". When set, the app
    # connects to whichever of them is writable (libpq target_session_attrs=read-write), so a
    # failover needs no change here. Empty = the host in the database URL only.
    database_hosts: str = ""
    # E10-S11: where snapshot.sh keeps the dumps + object copies, and the restore drill reports
    backup_dir: str = "/workspace/backup"
    dr_report_dir: str = "/workspace/logs/dr"
    redis_url: str = Field(default="redis://127.0.0.1:6379/0", repr=False)
    # E9-S8: the Celery broker. Empty = redis_url, which is what every deployment uses today; an
    # amqp:// URL moves the broker to RabbitMQ with no code change (the queues, routing, lanes and
    # tasks are the same), which is what makes the broker-durability comparison a configuration
    # swap. The result backend stays on Redis either way, so the drill compares brokers only.
    broker_url: str = Field(default="", repr=False)

    # object store (MinIO)
    s3_endpoint_url: str = "http://127.0.0.1:9000"
    s3_access_key: str = Field(default="minioadmin", repr=False)
    s3_secret_key: str = Field(default="minioadmin", repr=False)
    s3_bucket: str = "cdi-documents"

    # secrets store for the four credentials above (backlog E1-S5, docs/secrets.md)
    secrets_dir: str | None = None

    # folder-watch intake
    inbox_dir: str = "/workspace/data/inbox"
    processed_dir: str = "/workspace/data/processed"
    failed_dir: str = "/workspace/data/failed"

    # S1 render resolution
    page_dpi: int = Field(default=200, ge=50, le=600)

    # web app (the RunPod-proxied port)
    webapp_port: int = 8888
    # E10-S9: with several instances each binds 127.0.0.1:<own port> and the balancer
    # (infra/runpod/Caddyfile.lb) holds the public port; a single instance keeps 0.0.0.0
    webapp_host: str = "0.0.0.0"  # noqa: S104 - the pod proxy / balancer reaches it
    # names this process in /readyz, logs and web_job.accepted_by; empty = <hostname>:<port>
    instance_name: str = ""

    # durable jobs (backlog E10-S9, E9-S1 core; docs/ha.md "Application tier"): an upload is staged
    # in the object store and written to web_job before it is acknowledged. "queue" = the job runs
    # on a Celery worker (queue cdi-web, infra/runpod/start_worker.sh); "inprocess" = on the web app
    # instance that took the upload (dev / tests). Either way a job whose runner dies (its lease
    # runs out) is claimed and resumed by another runner.
    job_execution: Literal["inprocess", "queue"] = "inprocess"
    job_lease_s: float = Field(default=30.0, ge=3)
    # a job never claimed this long after its upload (its queue message lost) is claimed anyway
    job_unclaimed_s: float = Field(default=120.0, ge=5)
    job_max_attempts: int = Field(default=3, ge=1)
    # E9-S1 schema changes under load (cdi_adapter.migration): how long a migration waits for the
    # jobs in flight to end before it gives up (nothing applied), and the longest it may hold the
    # queue - a migrator that dies never gates new claims for more than this
    migration_drain_s: float = Field(default=120.0, ge=0)
    migration_hold_s: float = Field(default=900.0, ge=10)
    # a pipeline_run still "running" after this long is a worker that died, not one in flight,
    # and no longer holds a migration back
    migration_stage_stale_s: float = Field(default=600.0, ge=1)
    # E9-S2 pool split: which side of the pipeline this worker process serves. "cpu" runs the
    # ingest/OCR/classify stages and hands extraction to the GPU pool; "gpu" runs extraction;
    # "all" runs both in one worker (dev, a single-worker pod). "" = derive it from the queues the
    # worker consumes (cdi/cdi-web -> cpu, cdi-gpu -> gpu, both -> all), which is what the start
    # scripts rely on - so set this per worker process, never in a shared .env.
    worker_role: Literal["", "all", "cpu", "gpu"] = ""
    # how many documents one worker of each pool runs at once (Celery --concurrency). 0 = auto:
    # the CPU pool takes job_max_workers (Phase 1 is I/O- and OCR-bound), the GPU pool takes
    # extract_slots() (E2-S2, what the model gateway itself allows). Raising the CPU number buys
    # Phase-1 throughput and never touches GPU spend; that independence is the point of E9-S2.
    worker_cpu_concurrency: int = Field(default=0, ge=0)
    worker_gpu_concurrency: int = Field(default=0, ge=0)
    # Prometheus /metrics of a Celery web-job worker (127.0.0.1 only; 0 = off)
    worker_metrics_port: int = Field(default=9470, ge=0, le=65535)
    # E10-S9: a model gateway replica that refused a connection is skipped this long
    mlserve_down_s: float = Field(default=15.0, ge=0)
    # Prometheus /metrics of the web app (backlog E10-S1): its own port, bound to 127.0.0.1 only,
    # so the staff-facing app and the ingress never serve it; 0 = off
    metrics_port: int = Field(default=9464, ge=0, le=65535)
    # OpenTelemetry export (backlog E10-S3): empty = off (spans still exist, so logs keep their
    # trace ids). Pod: Tempo's OTLP receiver and Loki's OTLP ingestion, both on 127.0.0.1
    otel_traces_endpoint: str = ""  # e.g. http://127.0.0.1:4318/v1/traces
    otel_logs_endpoint: str = ""  # e.g. http://127.0.0.1:3100/otlp/v1/logs

    # model gateway. E10-S9: several replicas of the same model, comma separated; a call goes to
    # the next replica when one refuses the connection or answers 5xx
    mlserve_url: str = "http://127.0.0.1:8077"
    mlserve_port: int = 8077
    mlserve_backend: Literal["stub", "hf", "vllm"] = "stub"
    vlm_model_id: str = "Qwen/Qwen2.5-VL-7B-Instruct"
    vlm_fallback_model_id: str = "Qwen/Qwen2.5-VL-3B-Instruct"
    # model registry (backlog E10-S5): a gateway started for a registered version loads the model
    # at its pinned revision and reports the version on /healthz; the web app routes documents to
    # the primary or the canary gateway from the rollout log, re-read every model_route_ttl_s
    vlm_model_revision: str | None = None
    model_version: str | None = None
    model_route_ttl_s: float = Field(default=5.0, ge=0)
    vlm_max_pixels_ocr: int = 2_000_000
    # hf backend weights: "" = bf16 (ARCHITECTURE §3.4, the pod). "8bit" = bitsandbytes
    # LLM.int8, only for a test host below the 24 GB bar (docs/fallback-host.md): the 7B's bf16
    # weights (~16 GB) do not fit a 15 GB T4. start_mlserve.sh installs the pinned bitsandbytes.
    vlm_quantize: Literal["", "8bit"] = ""
    # vllm backend (backlog E2-S1, §3.4): 32 GB = ~15 GB weights + ~12-14 GB KV cache
    # -> 3-4 concurrent page extractions
    vllm_gpu_memory_utilization: float = Field(default=0.88, gt=0, le=0.98)
    vllm_max_model_len: int = Field(default=16384, ge=2048)
    vllm_max_num_seqs: int = Field(default=4, ge=1)
    # grammar-locked JSON decoding with XGrammar (backlog E2-S3, §14 #3)
    vllm_guided: bool = True
    vllm_request_timeout_s: float = Field(default=240.0, gt=0, allow_inf_nan=False)

    # pipeline behaviour
    fast_classify: bool = True
    # the rule-based fast path is for cleanly printed pages only: a page whose OCR is less sure than
    # this on average (handwriting read by a print OCR) goes to the VLM, which can say "handwritten"
    # and so send it through the VLM handwriting pass (2026-09-26: a printed letterhead with a
    # handwritten Rx, mean OCR 0.80, was taken as printed and its drugs extracted as gibberish)
    fast_classify_min_ocr_conf: float = Field(default=0.88, ge=0, le=1)
    # medicine catalogue layer (owner request 2026-09-26; docs/medicine-catalogue.md): after S5,
    # each medication fact is identified against the imported catalogue (Tata 1mg). A fuzzy match is
    # accepted only at >= min_score AND a lead of >= min_margin over every other brand; else
    # UNKNOWN_MEDICATION. Off, or an empty catalogue, changes nothing.
    catalogue_enabled: bool = True
    catalogue_min_score: float = Field(default=0.85, ge=0, le=1)
    catalogue_min_margin: float = Field(default=0.05, ge=0, le=1)
    catalogue_strength_must_agree: bool = True  # a fuzzy name with another strength: unknown
    # the medication agent (backlog E12-S4, ADR-0003): after validation it re-reads each medicine
    # line with the prescription's diagnoses and the catalogue and SUGGESTS the product written
    # there; advisory only, a reviewer applies it
    agent_enabled: bool = True
    agent_max_options: int = Field(default=10, ge=2, le=20)  # catalogue products per line
    agent_min_grounding: float = Field(default=0.45, ge=0, le=1)  # evidence text vs product name
    # the recall step (owner, 2026-09-28): the model is given each medicine line's extracted text
    # and the prescription's diagnoses and names, from its own knowledge, the brands and generics
    # the line most likely is; they widen the catalogue shortlist (false: the agent as before)
    agent_recall: bool = True
    extract_retries: int = Field(default=1, ge=0)
    job_max_workers: int = Field(default=6, ge=1)
    # Phase-2 extractions in flight at once (backlog E2-S2, §7.2): 0 = auto, i.e.
    # vllm_max_num_seqs with the vllm backend and 1 otherwise; hf is always 1
    extract_concurrency: int = Field(default=0, ge=0)
    # batch short documents of one doc_type into one gateway call (backlog E2-S8, §7.4): one
    # batched forward pass in which every document is its own sequence with its own prompt, so
    # evidence can never cross documents. "Short" = one page and at most extract_batch_max_blocks
    # OCR blocks. 1 = off, the default until the E1-S7 A/B on the GPU shows a gain.
    extract_batch_max: int = Field(default=1, ge=1, le=8)
    extract_batch_max_blocks: int = Field(default=40, ge=1)
    ocr_engine: Literal["rapidocr", "vlm"] = "rapidocr"
    handwritten_uses_vlm: bool = True
    ocr_min_conf: float = Field(default=0.30, ge=0, le=1)  # §5 S3: RapidOCR line kept if >= this

    # S6 governance gate (§5 S6, §14 "The gate"); per-class fitted thresholds are backlog E4-S3
    gate_auto_accept_conf: float = Field(default=0.985, ge=0, le=1)
    gate_audit_conf: float = Field(default=0.95, ge=0, le=1)
    gate_review_floor: float = Field(default=0.85, ge=0, le=1)
    gate_partial_penalty: float = Field(default=0.30, ge=0, le=1)
    audit_sample_rate: float = Field(default=0.05, ge=0, le=1)
    # E4-S2: the fitted calibrator the gate decides with (a calibration_model version). Unset:
    # the fixed-penalty _calibrate score, as before. Only set it to a calibrator fitted on real
    # clinician-adjudicated data (E14-S1).
    calibrator_version: int | None = Field(default=None, ge=1)
    # E4-S3: the derived per-class gate thresholds (a checked-in gate_thresholds.v1 JSON file, on
    # the scale of CDI_CALIBRATOR_VERSION). Unset: the global constants above, as before.
    gate_thresholds: str | None = None

    # jurisdiction packages (§2)
    # the NRCeS ABDM IG as published in the FHIR package registry: id "ndhm.in" (canonical
    # https://nrces.in/ndhm/fhir/r4); "nrces.fhir.r4.ndhm" is not a registry id (backlog E6-S1)
    ig_package: str = "ndhm.in#6.5.0"
    terminology_package: str = "in-snomed-loinc-icd10"

    # HL7 FHIR validator jar (TECH_STACK §10): used when java + this jar + the pinned package
    # cache exist (backlog E6-S1). The cache holds the IG tarball and every package it pulls in,
    # frozen and checked against infra/fhir/packages.lock; validation never goes online (§12).
    fhir_validator_jar: str = "/workspace/tools/validator_cli.jar"
    fhir_java: str = "java"
    fhir_package_home: str = "/workspace/tools/fhir-home"

    # web-app sign-in (backlog E11-S1, ARCHITECTURE §12): server-side sessions + account lockout.
    # The cookie is Secure by default (staff reach the app over TLS: E1-S9 ingress / RunPod proxy);
    # set CDI_SESSION_COOKIE_SECURE=false only for plain-http local development.
    session_idle_minutes: int = Field(default=30, ge=1)
    session_max_hours: int = Field(default=12, ge=1)
    session_cookie_secure: bool = True
    login_max_failures: int = Field(default=5, ge=1)
    login_lockout_minutes: int = Field(default=15, ge=1)
    # Every unsafe request must be same-origin: Origin/Referer host == Host (webapp.auth). The
    # E1-S9 ingress passes Host through, so staff traffic matches and this stays empty. A proxy
    # that REWRITES Host (Colab's port proxy, RunPod's <POD_ID>-8888.proxy.runpod.net) makes the
    # two disagree and every POST is refused; name those hostnames here, comma separated, to
    # accept them as well. An explicit list only - no wildcards, never a public deployment.
    public_origins: str = ""

    # de-identification before any training / evaluation use (backlog E11-S4, ARCHITECTURE §12):
    # Presidio + spaCy + a clinical de-id NER, loaded from local files at a pinned revision only
    deid_spacy_model: str = "en_core_web_lg"
    deid_ner_model: str = "obi/deid_roberta_i2b2"
    deid_ner_revision: str = "78f2152eb93ddd817290ce8dbe46f1a6685e09fc"
    deid_min_score: float = Field(default=0.35, ge=0, le=1)


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings, read once at start-up.

    Credentials come from the secrets store (backlog E1-S5, ``cdi_adapter.secretstore``) when one
    is configured; a store readable by group/other refuses to load.
    """
    from cdi_adapter import secretstore

    base = Settings()  # env + .env only: tells us where the store is
    store = secretstore.active_dir(base.secrets_dir)
    if store is None:
        return base
    if not store.is_dir():
        raise FileNotFoundError(f"{secretstore.ENV_VAR}={store} does not exist")
    secretstore.assert_safe(store)
    problems = secretstore.check(store, names=tuple(f"cdi_{f}" for f in secretstore.FIELDS))
    if problems:
        raise ValueError("invalid secrets store: " + "; ".join(problems))
    credentials = {field: secretstore.read(store, f"cdi_{field}") for field in secretstore.FIELDS}
    # A configured vault/store is authoritative for credentials; env still configures other fields.
    return Settings(**credentials, _secrets_dir=store)  # type: ignore[arg-type,call-arg]
