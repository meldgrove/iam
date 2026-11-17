import pytest
from django.urls import reverse

from iam.lifecycle import ALLOWED_FROM, start_workflow
from iam.models import (
    AccessProfile,
    AuditEvent,
    Connection,
    Job,
    Person,
    ProfileGrant,
    Role,
    Task,
    Team,
    User,
    Workflow,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(person, fake_connection, approver_user, fake_store):
    workflow = start_workflow(person, Workflow.Kind.ONBOARDING, actor=approver_user)
    task = Task.objects.create(kind=Task.Kind.CHANGE, title="Do something", person=person)
    return {"workflow": workflow, "job": workflow.jobs.get(), "task": task}


def all_pages(world, person, fake_connection, profile, team, admin_user):
    return [
        reverse("dashboard"),
        reverse("person_list"),
        reverse("person_list") + "?q=ada&status=onboarding",
        reverse("person_detail", args=[person.pk]),
        reverse("team_list"),
        reverse("team_detail", args=[team.pk]),
        reverse("profile_list"),
        reverse("profile_detail", args=[profile.pk]),
        reverse("workflow_list"),
        reverse("workflow_list") + "?status=all",
        reverse("workflow_detail", args=[world["workflow"].pk]),
        reverse("job_list"),
        reverse("job_list") + "?status=all",
        reverse("job_detail", args=[world["job"].pk]),
        reverse("task_list"),
        reverse("task_detail", args=[world["task"].pk]),
        reverse("audit_list") + "?q=onboarding",
    ]


ADMIN_PAGES = [
    "connection_list",
    "connection_choose",
    "user_list",
    "user_create",
    "settings",
    "team_create",
    "profile_create",
]


def test_every_page_renders_for_viewers(
    client_for, viewer_user, world, person, fake_connection, profile, team, admin_user
):
    client = client_for(viewer_user)
    for url in all_pages(world, person, fake_connection, profile, team, admin_user):
        response = client.get(url)
        assert response.status_code == 200, url
        assert response["Cache-Control"] == "no-store"
        assert "default-src 'self'" in response["Content-Security-Policy"]
    for name in ADMIN_PAGES:
        assert client.get(reverse(name)).status_code == 403, name
    assert client.get(reverse("person_create")).status_code == 403


def test_admin_pages_render(client_for, admin_user, fake_connection, world, groups):
    client = client_for(admin_user)
    for name in ADMIN_PAGES:
        assert client.get(reverse(name)).status_code == 200, name
    for slug in [
        "keycloak",
        "google_workspace",
        "scim",
        "hubspot",
        "slack",
        "notion",
        "relution",
        "manual",
        "personio",
        "smtp",
    ]:
        assert client.get(reverse("connection_create", args=[slug])).status_code == 200, slug
    detail = client.get(reverse("connection_detail", args=[fake_connection.pk]))
    assert detail.status_code == 200
    assert b"fake-token" not in detail.content
    assert client.get(reverse("connection_edit", args=[fake_connection.pk])).status_code == 200
    assert client.get(reverse("team_edit", args=[Team.objects.get().pk])).status_code == 200
    assert (
        client.get(reverse("profile_edit", args=[AccessProfile.objects.get().pk])).status_code
        == 200
    )
    assert client.get(reverse("user_edit", args=[admin_user.pk])).status_code == 200


def test_viewers_cannot_change_anything(client_for, viewer_user, world, person):
    client = client_for(viewer_user)
    for url in [
        reverse("person_start", args=[person.pk, "offboarding"]),
        reverse("task_complete", args=[world["task"].pk]),
        reverse("job_retry", args=[world["job"].pk]),
        reverse("workflow_cancel", args=[world["workflow"].pk]),
    ]:
        assert client.post(url).status_code == 403, url


def test_approvers_cannot_approve_removals_or_manage_connections(
    client_for, approver_user, world, fake_connection
):
    client = client_for(approver_user)
    assert client.post(reverse("job_approve", args=[world["job"].pk])).status_code == 403
    assert client.get(reverse("connection_list")).status_code == 403
    assert client.post(reverse("connection_test", args=[fake_connection.pk])).status_code == 403


def test_add_person_uses_the_default_profile_and_audits(client_for, approver_user, profile):
    client = client_for(approver_user)
    response = client.post(
        reverse("person_create"),
        {
            "first_name": "Grace",
            "last_name": "Hopper",
            "work_email": "Grace@Example.invalid",
            "person_type": "employee",
        },
    )
    person = Person.objects.get(last_name="Hopper")
    assert response.status_code == 302
    assert person.work_email == "grace@example.invalid"
    assert person.access_profile == profile
    assert AuditEvent.objects.filter(
        action="person.created", person=person, actor=approver_user
    ).exists()


def test_end_date_before_start_is_refused(client_for, approver_user):
    response = client_for(approver_user).post(
        reverse("person_create"),
        {
            "first_name": "A",
            "last_name": "B",
            "person_type": "employee",
            "start_date": "2026-12-01",
            "end_date": "2026-11-01",
        },
    )
    assert response.status_code == 200 and b"cannot be before" in response.content


def test_start_workflow_from_the_person_page(client_for, approver_user, person, fake_store):
    client = client_for(approver_user)
    response = client.post(reverse("person_start", args=[person.pk, "onboarding"]))
    workflow = Workflow.objects.get()
    assert response["Location"] == reverse("workflow_detail", args=[workflow.pk])
    assert workflow.created_by == approver_user
    response = client.post(reverse("person_start", args=[person.pk, "access_update"]), follow=True)
    assert response.status_code == 200


@pytest.mark.parametrize("status", Person.Status.values)
def test_the_person_page_offers_what_lifecycle_allows(client_for, approver_user, person, status):
    Person.objects.filter(pk=person.pk).update(status=status)
    page = (
        client_for(approver_user).get(reverse("person_detail", args=[person.pk])).content.decode()
    )
    for kind in Workflow.Kind.values:
        offered = f'action="{reverse("person_start", args=[person.pk, kind])}"' in page
        assert offered == (status in ALLOWED_FROM[kind]), kind
    archive = f'action="{reverse("person_archive", args=[person.pk])}"' in page
    assert archive == (status in {Person.Status.PLANNED, Person.Status.OFFBOARDED})


def test_offboarded_people_can_be_offboarded_again_from_their_page(
    client_for, approver_user, person, fake_store
):
    # Fixing drift: an offboarded person still has an active account somewhere.
    Person.objects.filter(pk=person.pk).update(status=Person.Status.OFFBOARDED)
    client = client_for(approver_user)
    start = reverse("person_start", args=[person.pk, "offboarding"])
    page = client.get(reverse("person_detail", args=[person.pk])).content.decode()
    assert f'action="{start}"' in page
    client.post(start)
    assert Workflow.objects.get(kind=Workflow.Kind.OFFBOARDING).is_open


def test_complete_task_from_its_page(client_for, approver_user, world):
    client = client_for(approver_user)
    client.post(reverse("task_complete", args=[world["task"].pk]), {"note": "done by hand"})
    world["task"].refresh_from_db()
    assert world["task"].status == Task.Status.DONE and world["task"].note == "done by hand"


def test_connection_secrets_are_write_only(client_for, admin_user):
    client = client_for(admin_user)
    response = client.post(
        reverse("connection_create", args=["keycloak"]),
        {
            "name": "Keycloak",
            "mode": "automatic",
            "cfg_base_url": "https://sso.example.invalid",
            "cfg_realm": "example",
            "cfg_client_id": "iam-admin",
            "cfg_client_secret": "very-secret-value",
            "cfg_username_source": "username",
            "cfg_setup_email_lifespan_hours": "72",
            "cfg_sync_interval_minutes": "360",
            "cfg_send_setup_email": "on",
        },
    )
    assert response.status_code == 302, response.content.decode()[:2000]
    connection = Connection.objects.get(name="Keycloak")
    assert "very-secret-value" not in connection.secret_data
    assert connection.secrets() == {"client_secret": "very-secret-value"}
    assert "client_secret" not in connection.settings
    assert connection.settings["send_setup_email"] is True and not connection.enabled
    edit = client.get(reverse("connection_edit", args=[connection.pk]))
    assert b"very-secret-value" not in edit.content
    assert b"Leave empty to keep it" in edit.content
    client.post(
        reverse("connection_edit", args=[connection.pk]),
        {
            "name": "Keycloak",
            "enabled": "on",
            "mode": "automatic",
            "cfg_base_url": "https://sso.example.invalid",
            "cfg_realm": "other",
            "cfg_client_id": "iam-admin",
            "cfg_client_secret": "",
            "cfg_username_source": "username",
            "cfg_setup_email_lifespan_hours": "72",
            "cfg_sync_interval_minutes": "360",
        },
    )
    connection.refresh_from_db()
    assert connection.enabled and connection.settings["realm"] == "other"
    assert connection.secrets() == {"client_secret": "very-secret-value"}
    for event in AuditEvent.objects.filter(action__startswith="connection."):
        assert "very-secret-value" not in str(event.details) + event.summary
    changed = AuditEvent.objects.get(action="connection.updated").details["settings_changed"]
    assert "realm" in changed and "client_secret" not in changed


def test_saved_secrets_do_not_follow_a_changed_url(client_for, admin_user, fake_connection):
    # Otherwise an admin session could send a stored secret to any server.
    client = client_for(admin_user)
    url = reverse("connection_edit", args=[fake_connection.pk])
    data = {"name": "Fake app", "mode": "automatic", "cfg_sync_interval_minutes": "0"}
    assert client.post(url, {**data, "cfg_token": ""}).status_code == 302
    moved = {**data, "cfg_url": "https://elsewhere.example.invalid"}
    response = client.post(url, {**moved, "cfg_token": ""})
    assert response.status_code == 200 and b"Enter it again" in response.content
    fake_connection.refresh_from_db()
    assert fake_connection.settings.get("url", "") == ""
    assert client.post(url, {**moved, "cfg_token": "new-token"}).status_code == 302
    fake_connection.refresh_from_db()
    assert fake_connection.settings["url"] == "https://elsewhere.example.invalid"
    assert fake_connection.secrets() == {"token": "new-token"}
    # Unchanged, the saved secret stays.
    assert client.post(url, {**moved, "cfg_token": ""}).status_code == 302
    fake_connection.refresh_from_db()
    assert fake_connection.secrets() == {"token": "new-token"}


def test_smtp_password_does_not_follow_a_changed_host(client_for, admin_user):
    client = client_for(admin_user)
    data = {
        "name": "Mail",
        "cfg_host": "smtp.example.invalid",
        "cfg_port": "587",
        "cfg_security": "starttls",
        "cfg_username": "iam",
        "cfg_password": "mail-password",
        "cfg_from_address": "it@example.invalid",
    }
    assert client.post(reverse("connection_create", args=["smtp"]), data).status_code == 302
    connection = Connection.objects.get(name="Mail")
    url = reverse("connection_edit", args=[connection.pk])
    kept = {**data, "cfg_password": ""}
    for change in (
        {"cfg_host": "mail.example.invalid"},
        {"cfg_security": "none"},
        {"cfg_port": "25"},
    ):
        response = client.post(url, {**kept, **change})
        assert response.status_code == 200 and b"Enter it again" in response.content, change
    assert client.post(url, {**kept, "cfg_from_name": "IT team"}).status_code == 302
    connection.refresh_from_db()
    assert connection.secrets() == {"password": "mail-password"}


def test_unreadable_secrets_can_be_entered_again(client_for, admin_user, fake_connection, settings):
    # A lost ENCRYPTION_KEY must not turn the edit form into an error page.
    from cryptography.fernet import Fernet

    settings.ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    client = client_for(admin_user)
    url = reverse("connection_edit", args=[fake_connection.pk])
    assert client.get(url).status_code == 200
    data = {"name": "Fake app", "mode": "automatic", "cfg_sync_interval_minutes": "0"}
    response = client.post(url, {**data, "cfg_token": ""})
    assert response.status_code == 200 and b"This field is required" in response.content
    assert client.post(url, {**data, "cfg_token": "new-token"}).status_code == 302
    fake_connection.refresh_from_db()
    assert fake_connection.secrets() == {"token": "new-token"}


def test_connection_test_records_the_status(client_for, admin_user, fake_connection, fake_store):
    client = client_for(admin_user)
    client.post(reverse("connection_test", args=[fake_connection.pk]))
    fake_connection.refresh_from_db()
    assert fake_connection.status == Connection.Status.OK
    assert "10 chars" in fake_connection.status_message
    from iam.adapters.base import ConfigurationError

    fake_store.fail_with = ConfigurationError("Token refused.")
    client.post(reverse("connection_test", args=[fake_connection.pk]))
    fake_connection.refresh_from_db()
    assert fake_connection.status == Connection.Status.ERROR
    assert fake_connection.status_message == "Token refused."


def test_import_groups_and_compare_from_the_connection_page(
    client_for, admin_user, fake_connection, fake_store
):
    client = client_for(admin_user)
    client.post(reverse("connection_import_groups", args=[fake_connection.pk]))
    assert fake_connection.groups.count() == 3
    client.post(reverse("connection_sync", args=[fake_connection.pk]))
    assert fake_connection.sync_runs.filter(kind="accounts", status="succeeded").exists()


def test_manual_groups_can_be_added_and_removed(client_for, admin_user):
    client = client_for(admin_user)
    connection = Connection.objects.create(
        adapter="manual", name="Figma", settings={"app_name": "Figma"}
    )
    client.post(reverse("connection_add_group", args=[connection.pk]), {"name": "Editors"})
    group = connection.groups.get()
    assert group.manual and group.name == "Editors"
    client.post(reverse("connection_remove_group", args=[connection.pk, group.pk]))
    assert not connection.groups.exists()


def test_profile_form_saves_grants_and_groups(client_for, admin_user, fake_connection, groups):
    client = client_for(admin_user)
    response = client.post(
        reverse("profile_create"),
        {
            "name": "Engineers",
            "default_for": ["intern"],
            "grant": [str(fake_connection.pk)],
            "groups": [str(groups["g-eng"].pk), str(groups["g-ops"].pk)],
        },
    )
    assert response.status_code == 302
    grant = ProfileGrant.objects.get(profile__name="Engineers")
    assert sorted(grant.groups.values_list("external_id", flat=True)) == ["g-eng", "g-ops"]
    profile = AccessProfile.objects.get(name="Engineers")
    client.post(reverse("profile_edit", args=[profile.pk]), {"name": "Engineers"})
    assert not profile.grants.exists()


def test_default_profile_per_type_is_unique(client_for, admin_user, profile):
    response = client_for(admin_user).post(
        reverse("profile_create"), {"name": "Other", "default_for": ["employee"]}
    )
    assert response.status_code == 200 and b"already the default" in response.content


def test_team_form_sets_groups(client_for, admin_user, groups):
    client = client_for(admin_user)
    client.post(reverse("team_create"), {"name": "Ops", "groups": [str(groups["g-ops"].pk)]})
    assert list(Team.objects.get(name="Ops").groups.values_list("external_id", flat=True)) == [
        "g-ops"
    ]


def test_admins_cannot_lock_themselves_out(client_for, admin_user):
    client = client_for(admin_user)
    client.post(
        reverse("user_edit", args=[admin_user.pk]),
        {"email": admin_user.email, "role": "viewer", "is_active": "on"},
    )
    admin_user.refresh_from_db()
    assert admin_user.role == Role.ADMIN


def test_add_user(client_for, admin_user):
    client_for(admin_user).post(
        reverse("user_create"),
        {"email": "New@Example.invalid", "role": "approver", "is_active": "on"},
    )
    user = User.objects.get(email="new@example.invalid")
    assert user.role == Role.APPROVER and not user.has_usable_password()


def test_settings_form(client_for, admin_user):
    client_for(admin_user).post(
        reverse("settings"),
        {
            "organization_name": "Example Org",
            "onboarding_lead_days": "3",
            "removal_limit_per_day": "2",
            "notification_emails": "it@example.invalid",
        },
    )
    from iam.models import AppSettings

    settings = AppSettings.load()
    assert settings.organization_name == "Example Org"
    assert not settings.automatic_onboarding
    assert settings.notification_recipients() == ["it@example.invalid"]


def test_held_jobs_are_approved_by_admins(client_for, admin_user, world):
    job = world["job"]
    Job.objects.filter(pk=job.pk).update(status=Job.Status.HELD)
    client_for(admin_user).post(reverse("job_approve", args=[job.pk]))
    job.refresh_from_db()
    assert job.status == Job.Status.QUEUED


def test_readiness_reports_a_stale_worker(client, settings):
    settings.HEALTHCHECK_TOKEN = "health-token"
    response = client.get("/-/readyz")
    assert response.status_code == 503 and "checks" not in response.json()
    response = client.get("/-/readyz", headers={"X-Health-Token": "health-token"})
    assert response.json()["checks"] == {"database": "ok", "worker": "stale"}
    from iam.models import WorkerHeartbeat

    WorkerHeartbeat.objects.create(name="worker")
    assert client.get("/-/readyz").status_code == 200


def test_a_team_filter_that_is_no_number_is_ignored(client_for, viewer_user, person):
    client = client_for(viewer_user)
    # "²" is a digit to str.isdigit() but no number to int().
    for url in ("/people/?team=abc", "/people/?team=²", "/jobs/?connection=²"):
        assert client.get(url).status_code == 200, url


def test_templates_have_no_style_attributes():
    # The Content Security Policy (style-src 'self') blocks them silently.
    from pathlib import Path

    import iam

    templates = Path(iam.__file__).parent / "templates"
    assert not [str(p) for p in templates.rglob("*.html") if "style=" in p.read_text()]


def test_scim_without_groups_offers_no_group_import(client_for, admin_user):
    connection = Connection.objects.create(
        adapter="scim", name="SCIM app", settings={"groups_enabled": False}
    )
    assert not connection.supports_groups and not connection.can_import_groups
    page = client_for(admin_user).get(reverse("connection_detail", args=[connection.pk]))
    assert b"Import groups" not in page.content
    with_groups = Connection.objects.create(
        adapter="scim", name="SCIM groups", settings={"groups_enabled": True}
    )
    assert with_groups.can_import_groups
    manual = Connection.objects.create(adapter="manual", name="Manual", settings={})
    assert manual.supports_groups and not manual.can_import_groups


def test_a_new_start_date_lifts_a_paused_onboarding(client_for, approver_user, person):
    Person.objects.filter(pk=person.pk).update(onboarding_paused=True)
    page = client_for(approver_user).get(reverse("person_detail", args=[person.pk]))
    assert b"The last onboarding was cancelled" in page.content
    response = client_for(approver_user).post(
        reverse("person_edit", args=[person.pk]),
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "work_email": "ada@example.invalid",
            "person_type": "employee",
            "team": person.team_id,
            "access_profile": person.access_profile_id,
            "start_date": "2027-01-04",
        },
    )
    assert response.status_code == 302
    person.refresh_from_db()
    assert not person.onboarding_paused
