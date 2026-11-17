"""An in-memory provisioning adapter for engine tests."""

import itertools

from iam.adapters.base import (
    AccountSpec,
    Adapter,
    AdapterError,
    EnsureResult,
    ExternalAccount,
    ExternalGroup,
    Field,
    TestResult,
    TransientError,
)


class FakeStore:
    def __init__(self):
        self.accounts: dict[str, dict] = {}
        self.groups = {"g-staff": "Staff", "g-eng": "Engineering", "g-ops": "Operations"}
        self.fail_with: Exception | None = None
        self.fail_group_with: Exception | None = None
        # Raised on every read of an account's groups while set.
        self.fail_group_read_with: Exception | None = None
        self.calls: list[str] = []
        self.ids = itertools.count(1)


STORE = FakeStore()


class FakeAdapter(Adapter):
    slug = "fake"
    name = "Fake app"
    supports_groups = True
    fields = (
        Field("url", "URL", kind="url", destination=True),
        Field("token", "Token", kind="secret", required=True),
        Field("sync_interval_minutes", "Compare every", kind="int", default=0),
    )
    password = None
    credentials_problem = ""

    def _maybe_fail(self, call):
        STORE.calls.append(call)
        if STORE.fail_with is not None:
            raise STORE.fail_with

    def test_connection(self):
        self._maybe_fail("test")
        return TestResult(True, f"Fake app with token {len(self.config.get('token', ''))} chars.")

    def list_groups(self):
        return [ExternalGroup(gid, name, path=f"/{name}") for gid, name in STORE.groups.items()]

    def _external(self, account_id):
        data = STORE.accounts[account_id]
        return ExternalAccount(
            external_id=account_id,
            username=data["username"],
            email=data["email"],
            active=data["active"],
            groups=frozenset(data["groups"]),
        )

    def find_account(self, spec: AccountSpec, external_id: str = ""):
        self._maybe_fail("find")
        if external_id in STORE.accounts:
            return self._external(external_id)
        for account_id, data in STORE.accounts.items():
            if data["username"] == spec.username:
                return self._external(account_id)
        return None

    def create_account(self, spec):
        account_id = f"u{next(STORE.ids)}"
        STORE.accounts[account_id] = {
            "username": spec.username,
            "email": spec.email,
            "active": True,
            "groups": set(),
        }
        STORE.calls.append(f"create:{spec.username}")
        return EnsureResult(
            account=self._external(account_id),
            created=True,
            initial_password=self.password,
            credentials_problem=self.credentials_problem,
        )

    def set_active(self, account, active):
        STORE.accounts[account.external_id]["active"] = active
        STORE.calls.append(f"active:{account.external_id}:{active}")
        return self._external(account.external_id)

    def account_groups(self, account):
        if STORE.fail_group_read_with is not None:
            raise STORE.fail_group_read_with
        return set(STORE.accounts[account.external_id]["groups"])

    def add_to_group(self, account, group_id):
        if STORE.fail_group_with is not None:
            error, STORE.fail_group_with = STORE.fail_group_with, None
            raise error
        STORE.accounts[account.external_id]["groups"].add(group_id)

    def remove_from_group(self, account, group_id):
        STORE.accounts[account.external_id]["groups"].discard(group_id)

    def list_accounts(self):
        self._maybe_fail("list")
        return [self._external(account_id) for account_id in STORE.accounts]


__all__ = ["STORE", "FakeAdapter", "AdapterError", "TransientError"]
