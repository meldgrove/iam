import time

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
