"""Did the broker lose or duplicate work? (backlog E9-S8; docs/ha.md, ARCHITECTURE §7.6)

E9-S1 chose Celery with a **Redis** broker and named RabbitMQ as the hardened option, which
leaves one question open: when the broker or a worker is killed mid-batch, does Redis lose a
document, or run one twice? This module is the measurement, kept out of the drill script so it can
be tested on its own: given the jobs an upload burst acknowledged, it counts - from PostgreSQL,
the only durable record - what was **lost** and what was **duplicated**, and says whether that
meets the agreed tolerance.

What the numbers mean (the definitions the drill is judged on):

* **lost** - an acknowledged document that never reached a terminal state: its job never finished,
  or the job finished with the document still ``pending``/``running``. A document that was simply
  *redelivered* and ran again is not lost; that is the queue doing its job.
* **given_up** - a document whose job was dead-lettered after ``CDI_JOB_MAX_ATTEMPTS`` (E9-S3).
  Reported on its own: the work did end, but it ended without a result, so it is never counted as
  a success and never hidden inside the loss figure.
* **duplicated** - a document processed more than once *into the results*: more than one
  ``extraction`` row, or more than one open review item. This is the number that matters after
  E9-S3, whose idempotency keys already stop a retried **upload** from becoming a second job -
  and ``jobs_sharing_a_key`` proves that, so a duplicate here is the broker's, not the client's.
* **resumed** - jobs that ran more than once (``attempts`` > 1). Information, not a failure: it is
  how many times a kill actually cost a take-over.

**Tolerance** (E9-S8's acceptance): zero lost documents, zero duplicated, zero given up. Nothing
else is a pass, and a run that fails it is the trigger to swap the broker to RabbitMQ
(``CDI_BROKER_URL``) and measure again.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

# E9-S8 acceptance: 0 lost, and duplicates only such as E9-S3 already de-duplicates (i.e. none
# left in the results). Changing these is changing what the story promises.
MAX_LOST = 0
MAX_DUPLICATED = 0
MAX_GIVEN_UP = 0


def _docs(conn: Connection, job_ids: list[str], where: str) -> list[dict[str, Any]]:
    """Every document of those jobs whose per-document state matches ``where`` (``d`` is the
    document object, ``j`` the job row)."""
    rows = conn.execute(
        text(
            "SELECT j.id AS job_id, j.state, j.attempts, j.finished_at, j.dead_lettered_at, "
            "       d->>'filename' AS filename, d->>'status' AS status, "
            "       d->>'document_id' AS document_id, d->>'error' AS error "
            "FROM web_job j, jsonb_array_elements(j.docs) AS d "
            f"WHERE j.id = ANY(:ids) AND ({where}) ORDER BY j.id"
        ),
        {"ids": job_ids},
    ).mappings()
    return [dict(r) for r in rows]


def lost(conn: Connection, job_ids: list[str]) -> list[dict[str, Any]]:
    """Acknowledged documents that never reached a terminal state."""
    if not job_ids:
        return []
    return _docs(conn, job_ids,
                 "j.finished_at IS NULL OR d->>'status' IN ('pending', 'running')")  # fmt: skip


def given_up(conn: Connection, job_ids: list[str]) -> list[dict[str, Any]]:
    """Documents whose job was dead-lettered: ended, but without a result (E9-S3)."""
    if not job_ids:
        return []
    return _docs(conn, job_ids, "j.dead_lettered_at IS NOT NULL")


def document_ids(conn: Connection, job_ids: list[str]) -> list[str]:
    """The documents those jobs ingested (a job that never got past ingest has none)."""
    if not job_ids:
        return []
    return [r["document_id"] for r in
            _docs(conn, job_ids, "d->>'document_id' IS NOT NULL")]  # fmt: skip


def duplicated(conn: Connection, doc_ids: list[str]) -> list[dict[str, Any]]:
    """Documents that reached the results more than once: a second extraction row, or a second
    open review item. One run of a document must leave exactly one of each."""
    if not doc_ids:
        return []
    rows = conn.execute(
        text(
            "WITH d AS (SELECT CAST(unnest(CAST(:ids AS text[])) AS uuid) AS id) "
            "SELECT d.id AS document_id, "
            "  (SELECT count(*) FROM extraction e WHERE e.document_id = d.id) AS extractions, "
            "  (SELECT count(*) FROM rv_item r WHERE r.source_document_id = d.id "
            "   AND r.status IN ('workbasket', 'claimed')) AS open_review_items "
            "FROM d"
        ),
        {"ids": doc_ids},
    ).mappings()
    return [dict(r) for r in rows if r["extractions"] > 1 or r["open_review_items"] > 1]


def jobs_sharing_a_key(conn: Connection, job_ids: list[str]) -> list[dict[str, Any]]:
    """Two jobs on one idempotency key - impossible while E9-S3's unique index holds, and checked
    so that a duplicate found above cannot be blamed on the client's retry."""
    if not job_ids:
        return []
    rows = conn.execute(
        text(
            "SELECT idempotency_key, count(*) AS jobs, array_agg(id) AS ids FROM web_job "
            "WHERE id = ANY(:ids) AND idempotency_key IS NOT NULL "
            "GROUP BY idempotency_key HAVING count(*) > 1"
        ),
        {"ids": job_ids},
    ).mappings()
    return [dict(r) for r in rows]


def resumed(conn: Connection, job_ids: list[str]) -> int:
    """How many of those jobs ran more than once (a kill cost a take-over)."""
    if not job_ids:
        return 0
    return int(conn.execute(
        text("SELECT count(*) FROM web_job WHERE id = ANY(:ids) AND attempts > 1"),
        {"ids": job_ids}).scalar_one())  # fmt: skip


def measure(conn: Connection, job_ids: list[str]) -> dict[str, Any]:
    """The whole measurement for one drill run, with its verdict against the tolerance."""
    docs = document_ids(conn, job_ids)
    out: dict[str, Any] = {
        "jobs": len(job_ids),
        "documents": len(docs),
        "lost": lost(conn, job_ids),
        "given_up": given_up(conn, job_ids),
        "duplicated": duplicated(conn, docs),
        "jobs_sharing_a_key": jobs_sharing_a_key(conn, job_ids),
        "resumed": resumed(conn, job_ids),
    }
    out["counts"] = {k: len(out[k]) for k in
                     ("lost", "given_up", "duplicated", "jobs_sharing_a_key")}  # fmt: skip
    out["tolerance"] = {"max_lost": MAX_LOST, "max_duplicated": MAX_DUPLICATED,
                        "max_given_up": MAX_GIVEN_UP}  # fmt: skip
    out["ok"] = (
        out["counts"]["lost"] <= MAX_LOST
        and out["counts"]["duplicated"] <= MAX_DUPLICATED
        and out["counts"]["given_up"] <= MAX_GIVEN_UP
        and out["counts"]["jobs_sharing_a_key"] == 0
    )
    return out


def persistence(url: str) -> dict[str, Any]:
    """How durable the broker's own storage is while the drill runs - the configuration the
    numbers have to be read against (E9-S8 names "Redis's default persistence configuration").

    Redis is asked directly (``CONFIG GET``). RabbitMQ is not asked over AMQP: Celery declares
    its queues durable and the messages persistent, and ``confirm_publish`` (set for an amqp
    broker in ``cdi_adapter.worker``) is what makes a publish mean "the broker has it" - so what
    matters there is recorded as settings, not probed.
    """
    from cdi_adapter.worker import broker_kind

    kind = broker_kind(url)
    if kind != "redis":
        return {"broker": kind,
                "queues_durable": True, "messages_persistent": True,
                "confirm_publish": True, "note": "declared by Celery; not probed"}  # fmt: skip
    try:
        import redis

        # the sync client, typed loosely: redis-py's stubs type every call as sync-or-async
        c: Any = redis.Redis.from_url(url, socket_timeout=3)
        keys = ("appendonly", "appendfsync", "save", "dir", "appendfilename")
        conf = {k: c.config_get(k).get(k) for k in keys}
        info = c.info("persistence")
        return {"broker": "redis", **conf,
                "rdb_last_save_time": info.get("rdb_last_save_time"),
                "aof_enabled": info.get("aof_enabled"),
                "rdb_changes_since_last_save": info.get("rdb_changes_since_last_save")}  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - a drill report says why, it does not crash
        return {"broker": "redis", "error": f"{type(exc).__name__}: {exc}"}
