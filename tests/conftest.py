import httpx
import pytest


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    """Reads that adapters try again pause without sleeping in tests; the
    pauses they asked for are listed here."""
    asked = []
    monkeypatch.setattr("iam.adapters.base.pause", asked.append)
    return asked


@pytest.fixture
def mock_transport():
    """Build an httpx.MockTransport from a handler(request) -> httpx.Response
    and record every request it saw in `.requests`."""

    def build(handler):
        seen = []

        def record(request):
            seen.append(request)
            return handler(request)

        transport = httpx.MockTransport(record)
        transport.requests = seen
        return transport

    return build


@pytest.fixture
def fake_store(monkeypatch):
    from iam import adapters

    from . import fakes

    monkeypatch.setitem(adapters._ADAPTERS, "fake", ("tests.fakes", "FakeAdapter"))
    fakes.STORE.__init__()
    fakes.FakeAdapter.password = None
    fakes.FakeAdapter.credentials_problem = ""
    return fakes.STORE


@pytest.fixture
def fake_connection(db, fake_store):
    from iam.models import Connection

    connection = Connection(adapter="fake", name="Fake app", enabled=True, settings={})
    connection.set_secrets({"token": "fake-token"})
    connection.save()
    return connection


@pytest.fixture
def groups(fake_connection):
    from iam.models import ProviderGroup

    return {
        gid: ProviderGroup.objects.create(connection=fake_connection, external_id=gid, name=name)
        for gid, name in [("g-staff", "Staff"), ("g-eng", "Engineering"), ("g-ops", "Operations")]
    }


@pytest.fixture
def profile(fake_connection, groups):
    from iam.models import AccessProfile, ProfileGrant

    profile = AccessProfile.objects.create(name="Staff", default_for=["employee"])
    grant = ProfileGrant.objects.create(profile=profile, connection=fake_connection)
    grant.groups.add(groups["g-staff"])
    return profile


@pytest.fixture
def team(groups):
    from iam.models import Team

    team = Team.objects.create(name="Engineering")
    team.groups.add(groups["g-eng"])
    return team


@pytest.fixture
def person(profile, team):
    from iam.models import Person

    return Person.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        work_email="ada@example.invalid",
        personal_email="ada@personal.example.invalid",
        access_profile=profile,
        team=team,
    )


def make_user(role, email=None):
    from iam.models import User

    return User.objects.create_user(email=email or f"{role}@example.invalid", role=role)


@pytest.fixture
def admin_user(db):
    return make_user("admin")


@pytest.fixture
def approver_user(db):
    return make_user("approver")


@pytest.fixture
def viewer_user(db):
    return make_user("viewer")


@pytest.fixture
def client_for(client):
    def login_as(user):
        client.force_login(user, backend="iam.auth.OIDCBackend")
        return client

    return login_as
