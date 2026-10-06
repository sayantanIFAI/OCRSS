"""Structured (key-value / JSON) logging for pipeline events (TECH_STACK §12).

Events such as ``classified``, ``ocr_done``, ``extracted``, ``terminology_bound``, ``projected``,
``vlm_json_partial`` and ``job_doc_failed`` are emitted through :func:`get_logger`. Every line
carries the current ``trace_id`` / ``span_id`` and, when log export is on, is also shipped to
Loki over OTLP (backlog E10-S3, ``cdi_adapter.tracing``).
"""

from __future__ import annotations

import logging
import sys

import structlog

from cdi_adapter import tracing

_configured = False


def configure(level: str = "INFO", *, json: bool = True) -> None:
    """Configure structlog once per process. ``json=False`` gives a console renderer for dev."""
    global _configured
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            tracing.add_trace_ids,
            structlog.processors.format_exc_info,
            tracing.ship_to_otel,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    _configured = True


def get_logger(name: str | None = None, **initial: object) -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure()
    log = structlog.get_logger(name) if name else structlog.get_logger()
    return log.bind(**initial) if initial else log
