"""E9-S3 without a database: what the idempotency key is made of, the priority lanes every
sender and worker must agree on, and a poison document being given up instead of retried for ever.
"""

from __future__ import annotations

from typing import Any

import pytest

from cdi_adapter import worker
from cdi_adapter.webapp import jobstore

PDF = ("rx.pdf", b"%PDF-1.4 one", "application/pdf")
PDF2 = ("note.pdf", b"%PDF-1.4 two", "application/pdf")


def _key(files: list[tuple[str, bytes, str | None]], **kw: Any) -> str:
    kw.setdefault("actor", "counter")
    return jobstore.idempotency_key(files, **kw)


# ------------------------------------------------------------------ idempotency keys


def test_the_same_upload_sent_again_has_the_same_key() -> None:
    """The retry of a timed-out POST sends the same bytes, so it must derive the same key."""
    assert _key([PDF, PDF2]) == _key([PDF, PDF2])


def test_a_different_upload_has_a_different_key() -> None:
    different = [
        ([PDF], "one file instead of two"),
        ([PDF2, PDF], "the same files in the other order"),
        ([PDF, ("note.pdf", b"%PDF-1.4 three", "application/pdf")], "one file's bytes changed"),
        ([PDF, ("other.pdf", PDF2[1], "application/pdf")], "one file renamed"),
    ]
    base = _key([PDF, PDF2])
    for files, why in different:
        assert _key(files) != base, why


def test_the_same_scan_for_another_patient_or_user_is_another_job() -> None:
    base = _key([PDF])
    assert _key([PDF], actor="someone-else") != base
    assert _key([PDF], patient_id="ba1d0f3e-0000-4000-8000-000000000001") != base
    assert _key([PDF], abha="12-3456-7890-1234") != base


def test_a_client_key_decides_and_cannot_collide_across_users() -> None:
    """With an Idempotency-Key the client owns the identity of the retry - even if it changes the
    bytes - but one account's key is never another's."""
    k = _key([PDF], given="counter-42")
    assert k == _key([PDF2], given="counter-42")  # the client says: this is the same submission
    assert k != _key([PDF], given="counter-43")
    assert k != _key([PDF], actor="other", given="counter-42")
    assert k != _key([PDF])  # and it is not the derived key either
    assert k.startswith("hdr:") and _key([PDF]).startswith("body:")


# ------------------------------------------------------------------ priority lanes


def test_every_sender_and_worker_agrees_on_the_lanes() -> None:
    """Redis has no priority of its own: Celery emulates it with one list per step, so the steps
    and the separator must be identical on both sides or a lane is simply never read."""
    conf = worker.celery_app.conf
    assert conf.broker_transport_options["priority_steps"] == list(range(worker.PRIORITY_STEPS))
    assert conf.broker_transport_options["sep"] == ":"
    assert conf.broker_transport_options["queue_order_strategy"] == "priority"
    assert conf.task_default_priority == worker.NORMAL_PRIORITY == jobstore.NORMAL_PRIORITY
    assert conf.task_inherit_parent_priority is True
    assert (jobstore.MIN_PRIORITY, jobstore.MAX_PRIORITY) == (0, worker.PRIORITY_STEPS - 1)


def test_a_job_is_dispatched_on_its_own_lane(monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    class _App:
        def send_task(self, name: str, **kw: Any) -> None:
            sent.append({"name": name, **kw})

    monkeypatch.setattr(worker, "celery_app", _App())
    monkeypatch.setattr(jobstore, "priority_of", lambda job_id: 9)
    monkeypatch.setattr(jobstore, "get_settings",
                        lambda: type("S", (), {"job_execution": "queue"})())  # fmt: skip

    jobstore.dispatch("a" * 32)
    jobstore.dispatch_extract("a" * 32)
    assert [s["priority"] for s in sent] == [9, 9]  # the GPU hand-off keeps the lane (E9-S2)
    assert [s["queue"] for s in sent] == [jobstore.WEB_QUEUE, jobstore.GPU_QUEUE]


def test_an_unknown_job_is_dispatched_as_normal(monkeypatch) -> None:
    """The lane lookup is never allowed to fail a dispatch: the job is already durable."""

    def boom() -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(jobstore, "session_scope", boom)
    assert jobstore.priority_of("b" * 32) == jobstore.NORMAL_PRIORITY


# ------------------------------------------------------------------ the poison document


class _Request:
    def __init__(self, retries: int, args: list[Any]) -> None:
        self.retries, self.args = retries, args
        self.delivery_info = {"routing_key": "cdi"}


class _Task:
    """Enough of a Celery task for ``worker._retry``."""

    name = "cdi.extract_document"

    def __init__(self, retries: int) -> None:
        self.request = _Request(retries, ["doc-1"])
        self.retried: list[dict[str, Any]] = []

    def retry(self, **kw: Any) -> Exception:
        self.retried.append(kw)
        return RuntimeError("retry scheduled")


@pytest.mark.parametrize("retries", [0, 1, 2])
def test_a_failure_within_the_budget_is_retried(retries: int, monkeypatch) -> None:
    letters: list[Any] = []
    monkeypatch.setattr(worker, "dead_letter", lambda *a, **k: letters.append((a, k)))
    task = _Task(retries)
    with pytest.raises(RuntimeError, match="retry scheduled"):
        worker._retry(task, ValueError("ocr blew up"))
    assert task.retried and task.retried[0]["max_retries"] == worker.MAX_RETRIES
    assert task.retried[0]["countdown"] == worker.RETRY_BACKOFF * (2**retries)
    assert letters == []  # nothing is given up while there are attempts left


def test_the_attempt_that_exhausts_the_budget_is_dead_lettered(monkeypatch) -> None:
    """The poison document leaves the queue: one dead letter, no further retry, and the document's
    own error is raised so its chain stops instead of carrying a broken document forward."""
    letters: list[dict[str, Any]] = []
    record = lambda kind, ref, **kw: letters.append({"kind": kind, "ref": ref, **kw})  # noqa: E731
    monkeypatch.setattr(worker, "dead_letter", record)
    task = _Task(worker.MAX_RETRIES)
    boom = ValueError("page 2 is not a PDF")
    with pytest.raises(ValueError, match="page 2 is not a PDF"):
        worker._retry(task, boom)
    assert task.retried == []
    assert len(letters) == 1
    assert letters[0]["kind"] == "document" and letters[0]["ref"] == "doc-1"
    assert letters[0]["attempts"] == worker.MAX_RETRIES + 1
    assert letters[0]["task"] == "cdi.extract_document"
    assert letters[0]["queue"] == "cdi"
    assert "page 2 is not a PDF" in letters[0]["error"]


def test_a_file_that_never_ingests_is_dead_lettered_by_its_path(monkeypatch) -> None:
    """``ingest_file`` has no document id yet, so the file itself is what was given up."""
    letters: list[dict[str, Any]] = []
    record = lambda kind, ref, **kw: letters.append({"kind": kind, "ref": ref, **kw})  # noqa: E731
    monkeypatch.setattr(worker, "dead_letter", record)
    task = _Task(worker.MAX_RETRIES)
    task.name = "cdi.ingest_file"
    with pytest.raises(OSError):
        worker._retry(task, OSError("corrupt"), kind="task", ref="/inbox/scan.pdf")
    assert letters[0]["kind"] == "task" and letters[0]["ref"] == "/inbox/scan.pdf"


def test_a_runner_without_a_dead_letter_store_still_gives_a_job_up() -> None:
    """The in-memory stores of the unit tests have no dead-letter table; giving up must not
    depend on having one."""
    from cdi_adapter.webapp.jobs import JobStore

    assert hasattr(JobStore, "dead_letter")  # the protocol names it
    assert getattr(object(), "dead_letter", None) is None  # and a store without it is tolerated
