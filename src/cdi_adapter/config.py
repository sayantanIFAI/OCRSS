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
    redis_url: str = Field(default="redis://127.0.0.1:6379/0", repr=False)

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

    # model gateway
    mlserve_url: str = "http://127.0.0.1:8077"
    mlserve_port: int = 8077
    mlserve_backend: Literal["stub", "hf"] = "stub"
    vlm_model_id: str = "Qwen/Qwen2.5-VL-7B-Instruct"
    vlm_fallback_model_id: str = "Qwen/Qwen2.5-VL-3B-Instruct"
    vlm_max_pixels_ocr: int = 2_000_000

    # pipeline behaviour
    fast_classify: bool = True
    extract_retries: int = Field(default=1, ge=0)
    job_max_workers: int = Field(default=6, ge=1)
    ocr_engine: Literal["rapidocr", "vlm"] = "rapidocr"
    handwritten_uses_vlm: bool = True
    ocr_min_conf: float = Field(default=0.30, ge=0, le=1)  # §5 S3: RapidOCR line kept if >= this

    # S6 governance gate (§5 S6, §14 "The gate"); per-class fitted thresholds are backlog E4-S3
    gate_auto_accept_conf: float = Field(default=0.985, ge=0, le=1)
    gate_audit_conf: float = Field(default=0.95, ge=0, le=1)
    gate_review_floor: float = Field(default=0.85, ge=0, le=1)
    gate_partial_penalty: float = Field(default=0.30, ge=0, le=1)
    audit_sample_rate: float = Field(default=0.05, ge=0, le=1)

    # jurisdiction packages (§2)
    ig_package: str = "nrces.fhir.r4.ndhm#6.5.0"
    terminology_package: str = "in-snomed-loinc-icd10"

    # HL7 FHIR validator jar (TECH_STACK §10): used when java + this jar exist
    fhir_validator_jar: str = "/workspace/tools/validator_cli.jar"


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
    return Settings(_secrets_dir=store)  # type: ignore[call-arg]  # pydantic-settings init kwarg
