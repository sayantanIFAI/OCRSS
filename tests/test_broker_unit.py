"""E9-S8 without a broker: that swapping Redis for RabbitMQ really is a configuration change,
and that the kill schedule the drill uses is random but repeatable and always inside the burst.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cdi_adapter import worker
from cdi_adapter.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _drill():  # noqa: ANN202 - the drill script, imported as a module
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import broker_crash_drill

        return broker_crash_drill
    finally:
        sys.path.pop(0)


# ------------------------------------------------------------------ the broker is configuration


def test_without_a_broker_url_nothing_changes() -> None:
    """Every deployment that says nothing keeps the Redis broker E9-S1 chose."""
    s = Settings(redis_url="redis://127.0.0.1:6379/0")
    assert worker.broker_url(s) == "redis://127.0.0.1:6379/0"
    assert worker.broker_kind(worker.broker_url(s)) == "redis"


def test_a_broker_url_moves_the_broker_without_touching_the_code() -> None:
    s = Settings(redis_url="redis://127.0.0.1:6379/0",
                 broker_url="amqp://guest:guest@127.0.0.1:5672//")  # fmt: skip
    assert worker.broker_url(s) == "amqp://guest:guest@127.0.0.1:5672//"
    assert worker.broker_kind(worker.broker_url(s)) == "amqp"


@pytest.mark.parametrize("url,kind", [
    ("redis://127.0.0.1:6379/0", "redis"),
    ("rediss://cache:6380/1", "redis"),
    ("amqp://guest:guest@127.0.0.1:5672//", "amqp"),
    ("amqps://user:pw@rabbit:5671/cdi", "amqp"),
    ("pyamqp://rabbit//", "amqp"),
])  # fmt: skip
def test_the_broker_kind_comes_from_the_url(url: str, kind: str) -> None:
    assert worker.broker_kind(url) == kind


def test_a_broker_this_project_does_not_run_is_refused() -> None:
    """Kafka is out of scope by decision (E9-S8 notes), so an unknown broker fails loudly rather
    than silently losing the priority lanes."""
    with pytest.raises(ValueError, match="Redis or RabbitMQ"):
        worker.broker_kind("kafka://127.0.0.1:9092")


def test_redis_keeps_the_emulated_priority_lanes() -> None:
    conf = worker.broker_conf("redis://127.0.0.1:6379/0")
    opts = conf["broker_transport_options"]
    assert opts["priority_steps"] == list(range(worker.PRIORITY_STEPS))
    assert opts["sep"] == ":" and opts["queue_order_strategy"] == "priority"
    assert "task_queue_max_priority" not in conf  # Redis has no real priority queue


def test_rabbitmq_gets_real_priority_queues_and_publisher_confirms() -> None:
    """The durability difference the drill measures: with confirm_publish a publish only returns
    once the broker has the message."""
    conf = worker.broker_conf("amqp://guest:guest@127.0.0.1:5672//")
    assert conf["task_queue_max_priority"] == worker.PRIORITY_STEPS - 1
    assert conf["broker_transport_options"] == {"confirm_publish": True}


def test_the_lanes_and_queues_do_not_depend_on_the_broker() -> None:
    """What E9-S8 promises: a swap is configuration, so the queues, routing and lanes are the
    same on both brokers - only how priority is expressed differs."""
    conf = worker.celery_app.conf
    assert conf.task_routes["cdi.extract_document"] == {"queue": worker.GPU_QUEUE}
    assert conf.task_default_priority == worker.NORMAL_PRIORITY
    assert conf.task_acks_late is True and conf.task_reject_on_worker_lost is True
    assert worker.queues_for_role("all") == "cdi,cdi-web,cdi-gpu"


def test_the_result_backend_stays_on_redis() -> None:
    """So the comparison is about the broker, and not about an AMQP result backend Celery has
    deprecated anyway."""
    assert worker.celery_app.conf.result_backend.startswith("redis")


# ------------------------------------------------------------------ the kill schedule


def test_the_kills_are_random_but_repeatable() -> None:
    d = _drill()
    a = d.kill_schedule(3, 60.0, seed=7)
    assert a == d.kill_schedule(3, 60.0, seed=7)  # a run that found a loss can be replayed
    assert a != d.kill_schedule(3, 60.0, seed=8)  # and a different seed kills elsewhere


def test_every_kill_lands_inside_the_burst() -> None:
    """A kill before the first upload or after the last proves nothing about "mid-batch"."""
    d = _drill()
    for seed in range(25):
        times = d.kill_schedule(3, 60.0, seed=seed, margin_s=2.0)
        assert len(times) == 3
        assert times == sorted(times)
        assert all(2.0 <= t <= 58.0 for t in times), times
        assert all(b - a >= 0.25 for a, b in zip(times, times[1:], strict=False))


def test_no_kill_is_asked_of_a_burst_too_short_for_one() -> None:
    d = _drill()
    assert d.kill_schedule(0, 60.0, seed=1) == []
    with pytest.raises(ValueError, match="too short"):
        d.kill_schedule(1, 2.0, seed=1, margin_s=2.0)


def test_the_drill_defaults_to_the_e9_s6_scenario() -> None:
    """E9-S8's acceptance is measured during the E9-S6 load test: 30 patients x 5-10 documents,
    and at least three kills across the runs."""
    d = _drill()
    a = d.parse([])
    assert (a.patients, a.docs_min, a.docs_max) == (30, 5, 10)
    assert a.kills >= 3 or a.runs * a.kills >= 3
    assert a.label == "redis"  # the broker in use today is what a plain run is evidence for


def test_the_drill_refuses_a_run_that_would_kill_nothing() -> None:
    d = _drill()
    assert d.main(["--kills", "0"]) == 2


def test_the_tolerance_is_the_one_the_story_agreed() -> None:
    from cdi_adapter import durability

    assert (durability.MAX_LOST, durability.MAX_DUPLICATED, durability.MAX_GIVEN_UP) == (0, 0, 0)
