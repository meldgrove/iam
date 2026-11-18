"""Reads that fail for a passing reason are tried again in place (ProviderClient)."""

import time
from email.utils import format_datetime

import httpx
import pytest
from django.utils import timezone

from iam import liveness
from iam.adapters.base import Adapter

URL = "https://app.example.invalid/users"


def client(transport):
    return Adapter({}, transport=transport).http()


def answers(*responses):
    queue = list(responses)

    def handler(request):
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return handler


def test_a_read_is_tried_again_after_a_passing_failure(mock_transport, pauses):
    transport = mock_transport(
        answers(httpx.Response(503), httpx.Response(502), httpx.Response(200))
    )
    assert client(transport).get(URL).status_code == 200
    assert len(transport.requests) == 3
    assert pauses == [1.0, 4.0]


def test_a_read_gives_up_after_three_tries(mock_transport, pauses):
    transport = mock_transport(lambda request: httpx.Response(504))
    assert client(transport).get(URL).status_code == 504
    assert len(transport.requests) == 3


@pytest.mark.parametrize("header", ["7", "date"])
def test_a_read_waits_as_long_as_the_server_asks(mock_transport, pauses, header):
    if header == "date":
        header = format_datetime(timezone.now() + timezone.timedelta(seconds=8), usegmt=True)
    transport = mock_transport(
        answers(httpx.Response(429, headers={"Retry-After": header}), httpx.Response(200))
    )
    assert client(transport).get(URL).status_code == 200
    assert len(pauses) == 1 and 6 <= pauses[0] <= 8


def test_a_long_retry_after_is_left_to_the_job(mock_transport, pauses):
    transport = mock_transport(lambda request: httpx.Response(429, headers={"Retry-After": "120"}))
    assert client(transport).get(URL).status_code == 429
    assert len(transport.requests) == 1 and not pauses


def test_a_refused_connection_is_tried_again(mock_transport, pauses):
    refused = httpx.ConnectError("refused")
    transport = mock_transport(answers(refused, httpx.Response(200)))
    assert client(transport).get(URL).status_code == 200
    transport = mock_transport(answers(refused, refused, refused))
    with pytest.raises(httpx.ConnectError):
        client(transport).get(URL)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_change_is_never_sent_twice(mock_transport, pauses, method):
    transport = mock_transport(lambda request: httpx.Response(503))
    assert client(transport).request(method, URL).status_code == 503
    assert len(transport.requests) == 1 and not pauses


def test_other_errors_are_not_tried_again(mock_transport, pauses):
    transport = mock_transport(lambda request: httpx.Response(500))
    assert client(transport).get(URL).status_code == 500
    assert len(transport.requests) == 1


def test_every_answer_counts_as_progress_for_the_watchdog(mock_transport, monkeypatch):
    monkeypatch.setattr(liveness, "_last_progress", time.monotonic() - 500)
    client(mock_transport(lambda request: httpx.Response(200))).get(URL)
    assert liveness.seconds_without_progress() < 5
