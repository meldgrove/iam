"""In-app updates: a small client for the Meldgrove Updater's HTTP API
(https://github.com/meldgrove/updater#the-apps-update-button). The updater
runs next to the app in compose.yaml at UPDATER_URL; it has no
authentication, so only admin views call it, and only with validated input."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from django.conf import settings

VERSION = re.compile(r"(?:[0-9]+\.[0-9]+\.[0-9]+(?:-preview\.[0-9]+)?|sha256:[0-9a-f]{12})")
CHANNELS = ("stable", "preview")
# Seconds per call: a status is local to the updater; a check (and a channel
# change, which checks) may take the updater up to 30 seconds.
STATUS_TIMEOUT = 3.0
CHECK_TIMEOUT = 35.0
INSTALL_TIMEOUT = 10.0
CHANNEL_TIMEOUT = 35.0
CONNECT_TIMEOUT = 3.0

# Tests replace this with an httpx.MockTransport.
transport: httpx.BaseTransport | None = None


@dataclass(frozen=True)
class Result:
    """What an updates route answers: the HTTP status and UpdatesResponse."""

    http_status: int
    reachable: bool
    updater: dict | None
    error: str | None = None

    @property
    def accepted(self) -> bool:
        """The updater did what was asked."""
        return self.reachable and 200 <= self.http_status < 300

    def body(self) -> dict:
        return {
            "app": {"version": settings.APP_VERSION or None},
            "reachable": self.reachable,
            "updater": self.updater,
            "error": self.error,
        }


def status() -> Result:
    return _call("GET", "/v1/status", timeout=STATUS_TIMEOUT)


def check() -> Result:
    return _call("POST", "/v1/check", timeout=CHECK_TIMEOUT)


def install(version: str) -> Result:
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        return refused("Name a version such as 0.2.1.")
    return _call("POST", "/v1/install", timeout=INSTALL_TIMEOUT, payload={"version": version})


def set_channel(channel: str) -> Result:
    if channel not in CHANNELS:
        return refused("The channel is stable or preview.")
    result = _call("POST", "/v1/channel", timeout=CHANNEL_TIMEOUT, payload={"channel": channel})
    # Saved only when the updater's status names the chosen channel.
    if result.accepted and (result.updater or {}).get("channel") != channel:
        return Result(409, True, result.updater, "The update service did not switch the channel.")
    return result


def refused(error: str) -> Result:
    """Input the app does not pass on: 400 with the current status."""
    current = status()
    return Result(400, current.reachable, current.updater, error)


def _call(method: str, path: str, *, timeout: float, payload: dict | None = None) -> Result:
    if not settings.UPDATER_URL:
        return Result(200, False, None, "No update service: UPDATER_URL is empty.")
    try:
        with httpx.Client(
            base_url=settings.UPDATER_URL,
            timeout=httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT)),
            transport=transport,
            follow_redirects=False,
            # The updater is a neighbour on the Compose network, never behind a proxy.
            trust_env=False,
        ) as client:
            response = client.request(method, path, json=payload)
    except httpx.TimeoutException:
        return Result(200, False, None, f"The update service did not answer within {timeout:g} s.")
    except httpx.HTTPError:
        return Result(200, False, None, f"No connection to {settings.UPDATER_URL}.")
    body = _json(response)
    if response.is_success:
        if not isinstance(body, dict) or "mode" not in body:
            return Result(200, False, None, "UPDATER_URL answers, but not as an update service.")
        return Result(response.status_code, True, body)
    # The updater refused (409: not offered, already installing, mode off,
    # channel locked) or failed: the admin sees its sentence and the latest status.
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, str) or not error.strip():
        error = f"The update service answered HTTP {response.status_code}."
    latest = status() if path != "/v1/status" else None
    # A failure (5xx) keeps its status code: the update button must not take
    # it for a refusal, since the updater may have started the installation.
    code = response.status_code if response.status_code >= 500 else 409
    return Result(code, True, latest.updater if latest and latest.reachable else None, error[:500])


def _json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return None
