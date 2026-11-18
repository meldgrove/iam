import time

import pytest

from iam import liveness


def test_the_watchdog_ends_a_stuck_worker(tmp_path, monkeypatch):
    alive = tmp_path / "alive"
    stopped = []
    liveness.note_progress()
    assert liveness.watch(60, alive_file=alive, stop=stopped.append)
    assert alive.exists() and not stopped
    touched = alive.stat().st_mtime
    monkeypatch.setattr(liveness, "_last_progress", time.monotonic() - 61)
    assert not liveness.watch(60, alive_file=alive, stop=stopped.append)
    assert stopped == [1]
    assert alive.stat().st_mtime == touched, "a stuck worker no longer looks healthy"


def test_the_watchdog_stops_the_worker_even_when_its_report_fails(tmp_path, monkeypatch):
    def broken(**kwargs):
        raise OSError("stderr is gone")

    monkeypatch.setattr(liveness.faulthandler, "dump_traceback", broken)
    monkeypatch.setattr(liveness, "_last_progress", time.monotonic() - 61)
    stopped = []
    with pytest.raises(OSError):
        liveness.watch(60, alive_file=tmp_path / "alive", stop=stopped.append)
    assert stopped == [1]
