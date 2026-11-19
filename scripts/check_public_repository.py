"""Reject private artifacts, unexpected contact domains and common credential formats.

Run against tracked files before sharing a revision. Dependency and integration
URLs below are intentional. This check cannot determine whether arbitrary prose
or synthetic-looking values contain confidential information; review those too.
"""

import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_HOSTS = {
    "admin.googleapis.com",
    "api.hubapi.com",
    "api.notion.com",
    "api.personio.de",
    "app.hubspot.com",
    "developers.google.com",
    "developers.hubspot.com",
    "developer.personio.de",
    "docs.docker.com",
    "docs.slack.dev",
    "files.pythonhosted.org",
    "github.com",
    "oauth2.googleapis.com",
    "pypi.org",
    "registry.npmjs.org",
    "slack.com",
    "www.googleapis.com",
    "www.keycloak.org",
    "www.rfc-editor.org",
    "www.w3.org",
}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "web", "keycloak", "mailpit", "postgres", "updater"}
PRIVATE_SUFFIXES = {".db", ".sqlite3", ".dump", ".eml", ".pem", ".key", ".log", ".tar", ".zip"}
# Binary files that are part of the repository on purpose.
REVIEWED_BINARIES = {"docs/screenshots"}
TOKEN = re.compile(
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|"
    r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}|"
    r"\bAKIA[A-Z0-9]{16}\b|\bxox[baprs]-[A-Za-z0-9-]{20,}"
)
URL = re.compile(r"\b(?:https?|postgresql)://[^\s\"'<>`)]+")
EMAIL = re.compile(r"\b[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})\b")


def check(path):
    name = path.relative_to(ROOT).as_posix()
    if path.suffix.lower() in PRIVATE_SUFFIXES or (
        path.name.startswith(".env") and not path.name.endswith(".example")
    ):
        return [f"{name}: private artifact type"]
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        if any(name.startswith(prefix + "/") for prefix in REVIEWED_BINARIES):
            return []
        return [f"{name}: binary asset requires review"]
    errors = []
    if TOKEN.search(content):
        errors.append(f"{name}: credential-shaped value")
    for match in EMAIL.finditer(content):
        host = match.group(1).lower()
        if host != "example.invalid" and not host.endswith(".example.invalid"):
            errors.append(f"{name}: non-example email domain")
    for match in URL.finditer(content):
        host = urlsplit(match.group()).hostname
        if (
            host
            and host.strip(".")
            and not (
                host in PUBLIC_HOSTS
                or host == "www.notion.so"
                or host.endswith(".slack.com")
                or host in {value.lower() for value in LOCAL_HOSTS}
                or host == "example.invalid"
                or host.endswith(".example.invalid")
            )
        ):
            errors.append(f"{name}: unexpected URL host")
    return errors


def main():
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    errors = [error for name in names if name for error in check(ROOT / name)]
    if errors:
        raise SystemExit("\n".join(sorted(set(errors))))
    print(f"Public repository checks passed for {len(names) - 1} tracked files.")


if __name__ == "__main__":
    main()
