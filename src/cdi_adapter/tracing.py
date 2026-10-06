"""OpenTelemetry tracing and log shipping (backlog E10-S3, ARCHITECTURE §10.4, §14 #9).

One trace follows a job: ``POST /api/jobs`` starts a ``job`` span, every pipeline stage of every
document is a child span (``stage.<name>``, attribute ``cdi.document_id``), the model gateway
continues the same trace from the W3C ``traceparent`` header, and the FHIR projection of the job,
a later request, is attached to the job's stored span context. Every structlog line carries
``trace_id`` / ``span_id`` (``add_trace_ids``).

Export is off unless configured, so tests and dev need no collector:

* ``CDI_OTEL_TRACES_ENDPOINT`` (pod: ``http://127.0.0.1:4318/v1/traces``, Tempo's OTLP receiver)
* ``CDI_OTEL_LOGS_ENDPOINT`` (pod: ``http://127.0.0.1:3100/otlp/v1/logs``, Loki's OTLP ingestion)

Spans are created either way, so trace ids appear in the logs even with nothing exported.
Attributes are ids and fixed names only: never a patient name, file name or document text (§12).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext

WEBAPP = "cdi-webapp"
MLSERVE = "cdi-mlserve"
WORKER = "cdi-worker"  # E10-S9: the Celery worker that runs web-app jobs

_provider: TracerProvider | None = None
_log_handler: logging.Handler | None = None


def setup(service: str, *, traces_endpoint: str = "", logs_endpoint: str = "") -> TracerProvider:
    """Install the SDK once per process. Later calls return the same provider (app factories and
    tests may run twice); an exporter is added only when its endpoint is set."""
    global _provider, _log_handler
    if _provider is not None:
        return _provider
    resource = Resource.create({"service.name": service, "service.namespace": "cdi-adapter"})
    provider = TracerProvider(resource=resource)
    if traces_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint)))
    trace.set_tracer_provider(provider)
    if logs_endpoint:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        lp = LoggerProvider(resource=resource)
        lp.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=logs_endpoint))
        )
        _log_handler = LoggingHandler(level=logging.INFO, logger_provider=lp)
    _provider = provider
    return provider


def setup_from_settings(service: str) -> TracerProvider:
    from cdi_adapter.config import get_settings

    s = get_settings()
    return setup(service, traces_endpoint=s.otel_traces_endpoint,
                 logs_endpoint=s.otel_logs_endpoint)  # fmt: skip


def tracer() -> trace.Tracer:
    return trace.get_tracer("cdi_adapter")


def ids(span: Span | None = None) -> dict[str, str]:
    """``trace_id`` / ``span_id`` (hex) of ``span`` or the current span; empty when none."""
    ctx = (span or trace.get_current_span()).get_span_context()
    if not ctx.is_valid:
        return {}
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}


# ------------------------------------------------------------------ structlog processors


def add_trace_ids(logger: Any, method: str, event: MutableMapping[str, Any]) -> Any:
    """Every log line carries the current trace and span id (E10-S3)."""
    for k, v in ids().items():
        event.setdefault(k, v)
    return event


_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
           "warn": logging.WARNING, "error": logging.ERROR, "critical": logging.CRITICAL,
           "exception": logging.ERROR}  # fmt: skip


def ship_to_otel(logger: Any, method: str, event: MutableMapping[str, Any]) -> Any:
    """When log export is on, the same event also goes to Loki over OTLP, with the current trace
    context attached by the OTel handler. Stdout output is unchanged."""
    if _log_handler is None:
        return event
    level = _LEVELS.get(method, logging.INFO)
    if level < _log_handler.level:
        return event
    record = logging.LogRecord(name=str(event.get("logger") or "cdi_adapter"), level=level,
                               pathname="", lineno=0, msg=json.dumps(event, default=str),
                               args=None, exc_info=None)  # fmt: skip
    try:
        _log_handler.emit(record)
    except Exception:  # noqa: BLE001 - shipping a log line must never break the caller
        pass
    return event


# ------------------------------------------------------------------ spans across threads / hops


@contextmanager
def span(name: str, attributes: Mapping[str, Any] | None = None,
         parent: SpanContext | None = None) -> Iterator[Span]:  # fmt: skip
    """A span that is the current span inside the block. ``parent`` continues a stored trace
    (the FHIR projection of a job, a later request)."""
    ctx = trace.set_span_in_context(NonRecordingSpan(parent)) if parent else None
    with tracer().start_as_current_span(name, context=ctx,
                                        attributes=dict(attributes or {})) as s:  # fmt: skip
        yield s


def start_job(job_id: str, parent: SpanContext | None = None) -> Span:
    """The span of a job's run; ended by the job runner when the run ends. A stored job (E10-S9)
    passes ``parent``, the upload's span, so the run continues the trace the upload started even
    in another process (and a resumed run joins the same trace)."""
    ctx = trace.set_span_in_context(NonRecordingSpan(parent)) if parent else None
    return tracer().start_span("job", context=ctx, attributes={"cdi.job_id": job_id})


def context_from_ids(trace_id: str | None, span_id: str | None) -> SpanContext | None:
    """The stored ``trace_id`` / ``span_id`` (hex, as :func:`ids` gives them) as a parent."""
    if not trace_id or not span_id:
        return None
    return SpanContext(int(trace_id, 16), int(span_id, 16), is_remote=True,
                       trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED))  # fmt: skip


@contextmanager
def attached(s: Span | None) -> Iterator[None]:
    """Make ``s`` the current span in this thread (worker threads do not inherit contextvars)."""
    if s is None:
        yield
        return
    token = otel_context.attach(trace.set_span_in_context(s))
    try:
        yield
    finally:
        otel_context.detach(token)


def inject_headers(headers: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """W3C ``traceparent`` for an outgoing call (web app -> model gateway)."""
    propagate.inject(headers)
    return headers


@contextmanager
def continued(headers: Mapping[str, str], name: str,
              attributes: Mapping[str, Any] | None = None) -> Iterator[Span]:  # fmt: skip
    """A server-side span continuing the caller's trace from its headers."""
    ctx = propagate.extract(dict(headers))
    with tracer().start_as_current_span(name, context=ctx, kind=trace.SpanKind.SERVER,
                                        attributes=dict(attributes or {})) as s:  # fmt: skip
        yield s
