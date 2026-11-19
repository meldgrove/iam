"""Answers captured from Meldgrove Updater 0.2 (tests/fixtures/updater-0.2) pass the
updater client unchanged."""

import json
from pathlib import Path

import httpx
import pytest

from iam import updates

FIXTURES = Path(__file__).parent / "fixtures" / "updater-0.2"
SAMPLES = sorted(path.name for path in FIXTURES.glob("*.json"))


def answering(updater, name):
    """Lets the fake updater answer every request with the sample, 202 for an installation."""

    def handle(request):
        code = 202 if request.url.path == "/v1/install" else 200
        return httpx.Response(code, content=(FIXTURES / name).read_bytes())

    updater.handler = handle
    return json.loads((FIXTURES / name).read_text())


def test_every_sample_is_here():
    assert len(SAMPLES) == 7


@pytest.mark.parametrize("name", SAMPLES)
def test_status_passes_the_sample_on(updater, name):
    expected = answering(updater, name)
    assert updates.status() == updates.Result(200, True, expected)
    assert updates.automatic_installs(expected)


def test_install_keeps_the_202(updater):
    expected = answering(updater, "install-202.json")
    result = updates.install("0.1.3")
    assert result == updates.Result(202, True, expected)
    assert result.accepted
