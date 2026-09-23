"""The scheduler loop must survive a job that raises (gotcha #44).

On 2026-08-13 02:58 UTC a `ConnectionResetError` from a Binance SSL handshake
escaped `position_manager` — registered as a bare `schedule` job — propagated
out of `schedule.run_pending()` and killed main(). The process did NOT exit:
`ThreadedWebsocketManager` runs non-daemon threads, so Docker saw a healthy
container while the bot did nothing for 2d 8h. An open SOLUSDT position sat with
an unmonitored stop that the market breached.

The container's `restart: unless-stopped` cannot help with a wedged-but-alive
process, so containment has to happen in-process.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest
import schedule

import main


def _force_due() -> None:
    """Backdate every job so `run_pending()` fires it on the next tick.

    `next_run = None` is not usable — `schedule.Job.should_run` asserts it is set.
    """
    past = datetime.now() - timedelta(seconds=1)
    for job in schedule.jobs:
        job.next_run = past


@pytest.fixture(autouse=True)
def _clean_schedule():
    """`schedule` keeps global state — isolate each test."""
    schedule.clear()
    yield
    schedule.clear()


def test_tick_contains_a_raising_job(caplog):
    calls = {"n": 0}

    def exploding_job():
        calls["n"] += 1
        raise ConnectionResetError(104, "Connection reset by peer")

    schedule.every(1).seconds.do(exploding_job)
    _force_due()

    with caplog.at_level(logging.ERROR):
        main.run_scheduler_tick()   # must not raise

    assert calls["n"] >= 1, "job should have been invoked"
    assert "Scheduled job raised" in caplog.text


def test_unguarded_sibling_starvation_is_the_reason_guarded_exists():
    """Document WHY per-job wrapping is needed, not just the outer catch.

    `schedule.run_pending()` aborts the whole pass on the first exception, so a
    job that fails every tick starves every job sorted after it. The outer catch
    in `run_scheduler_tick` keeps the loop alive but cannot prevent that.
    """
    state = {"boom": 0, "ok": 0}

    def boom():
        state["boom"] += 1
        raise RuntimeError("transient")

    schedule.every(1).seconds.do(boom)
    schedule.every(1).seconds.do(lambda: state.__setitem__("ok", state["ok"] + 1))

    for _ in range(3):
        _force_due()
        main.run_scheduler_tick()

    assert state["boom"] >= 3
    assert state["ok"] == 0, "unguarded: the sibling is starved — this is the bug"


def test_guarded_jobs_keep_siblings_running():
    """With `guarded`, a permanently-failing job cannot starve the others."""
    state = {"boom": 0, "ok": 0}

    def boom():
        state["boom"] += 1
        raise RuntimeError("transient")

    def ok():
        state["ok"] += 1

    schedule.every(1).seconds.do(main.guarded(boom, "boom"))
    schedule.every(1).seconds.do(main.guarded(ok, "ok"))

    for _ in range(3):
        _force_due()
        main.run_scheduler_tick()

    assert state["boom"] >= 3
    assert state["ok"] >= 3, "healthy jobs must keep running after a sibling fails"


def test_guarded_returns_value_and_passes_args():
    """`guarded` must be transparent for jobs that do not raise."""
    seen = {}

    def job(a, b=None):
        seen["args"] = (a, b)
        return "result"

    assert main.guarded(job, "job")(1, b=2) == "result"
    assert seen["args"] == (1, 2)


def test_tick_runs_a_normal_job_without_logging_errors(caplog):
    ran = {"n": 0}
    schedule.every(1).seconds.do(lambda: ran.__setitem__("n", ran["n"] + 1))
    _force_due()

    with caplog.at_level(logging.ERROR):
        main.run_scheduler_tick()

    assert ran["n"] == 1
    assert "Scheduled job raised" not in caplog.text
