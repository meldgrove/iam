"""Data model. The glossary in CONTEXT.md names each concept."""

from __future__ import annotations

import json

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Role(models.TextChoices):
    VIEWER = "viewer", "Viewer"
    APPROVER = "approver", "Approver"
    ADMIN = "admin", "Admin"


ROLE_RANK = {Role.VIEWER: 1, Role.APPROVER: 2, Role.ADMIN: 3}


class UserManager(BaseUserManager):
    def create_user(self, email, role=Role.VIEWER, **fields):
        user = self.model(email=self.normalize_email(email).lower(), role=role, **fields)
        user.set_unusable_password()
        user.save(using=self._db)
        return user


class User(AbstractBaseUser):
    """Someone who may use this app. Sign-in proves who they are; this record
    decides what they may do."""

    class RoleSource(models.TextChoices):
        LOCAL = "local", "Set in this app"
        PROVIDER = "provider", "Group claim of the sign-in provider"

    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=200, blank=True)
    oidc_subject = models.CharField(max_length=255, unique=True, null=True, blank=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.VIEWER)
    role_source = models.CharField(
        max_length=20, choices=RoleSource.choices, default=RoleSource.LOCAL
    )
    is_active = models.BooleanField(default=True)
    person = models.OneToOneField(
        "Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="app_user"
    )
    created_at = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    objects = UserManager()

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return self.display_name or self.email

    def has_role(self, role: str) -> bool:
        return self.is_active and ROLE_RANK.get(self.role, 0) >= ROLE_RANK[role]

    @property
    def is_admin(self) -> bool:
        return self.has_role(Role.ADMIN)

    @property
    def is_approver(self) -> bool:
        return self.has_role(Role.APPROVER)


class AppSettings(models.Model):
    """General settings an admin changes in Admin > Settings. One row."""

    organization_name = models.CharField(max_length=200, default="Meldgrove IAM")
    onboarding_lead_days = models.PositiveIntegerField(
        default=7,
        help_text="Days before the start date when onboarding starts automatically.",
    )
    automatic_onboarding = models.BooleanField(
        default=True, help_text="Start onboarding for planned people when their start date nears."
    )
    automatic_offboarding = models.BooleanField(
        default=True, help_text="Start offboarding the day after a person's end date."
    )
    removal_limit_per_day = models.PositiveIntegerField(
        default=5,
        help_text=(
            "Automatic account removals per connection and 24 hours. Removals beyond the "
            "limit wait for an admin's approval. 0 holds every automatic removal."
        ),
    )
    notification_emails = models.TextField(
        blank=True,
        help_text="Addresses that get an email for new manual tasks, one per line.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "settings"

    def __str__(self):
        return self.organization_name

    @classmethod
    def load(cls) -> AppSettings:
        instance, _ = cls.objects.get_or_create(pk=1)
        return instance

    def notification_recipients(self) -> list[str]:
        return [line.strip() for line in self.notification_emails.splitlines() if line.strip()]


class Connection(models.Model):
    """One connected app or service, configured in Admin > Connections."""

    class Mode(models.TextChoices):
        AUTOMATIC = "automatic", "Automatic: the app makes the changes"
        MANUAL = "manual", "Manual: the app creates tasks for each change"
        READ_ONLY = "read_only", "Read only: compare, never change"

    class Status(models.TextChoices):
        UNTESTED = "untested", "Not tested"
        OK = "ok", "Connected"
        ERROR = "error", "Error"

    adapter = models.CharField(max_length=50)
    name = models.CharField(max_length=100, unique=True)
    enabled = models.BooleanField(default=False)
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.AUTOMATIC)
    settings = models.JSONField(default=dict, blank=True)
    secret_data = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNTESTED)
    status_message = models.TextField(blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    groups_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def adapter_class(self):
        from .adapters import get_adapter_class

        return get_adapter_class(self.adapter)

    @property
    def kind(self) -> str:
        cls = self.adapter_class
        return cls.kind if cls else ""

    def secrets(self) -> dict:
        from .crypto import decrypt

        if not self.secret_data:
            return {}
        return json.loads(decrypt(self.secret_data))

    def set_secrets(self, values: dict) -> None:
        from .crypto import encrypt

        self.secret_data = encrypt(json.dumps(values)) if values else ""

    def config(self) -> dict:
        return {**self.settings, **self.secrets()}

    def build_adapter(self, **kwargs):
        cls = self.adapter_class
        if cls is None:
            raise LookupError(f"Unknown adapter {self.adapter!r}")
        return cls(self.config(), connection=self, **kwargs)

    @property
    def supports_groups(self) -> bool:
        cls = self.adapter_class
        return bool(cls and cls.groups_enabled(self.settings))

    @property
    def can_import_groups(self) -> bool:
        cls = self.adapter_class
        return bool(cls and cls.imports_groups() and self.supports_groups)

    @property
    def writes_automatically(self) -> bool:
        cls = self.adapter_class
        return bool(
            self.enabled
            and self.mode == self.Mode.AUTOMATIC
            and cls is not None
            and cls.supports_write
        )

    @property
    def sync_interval_minutes(self) -> int:
        try:
            return max(int(self.settings.get("sync_interval_minutes") or 0), 0)
        except (TypeError, ValueError):
            return 0


class ProviderGroup(models.Model):
    """A group, role, channel or team in a connected app that access can grant."""

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name="groups")
    external_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    path = models.CharField(max_length=500, blank=True)
    description = models.TextField(blank=True)
    # A group the provider no longer lists. Kept so policy shows what broke.
    missing = models.BooleanField(default=False)
    manual = models.BooleanField(default=False, help_text="Added by hand, not imported.")
    seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["connection__name", "path", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "external_id"], name="unique_group_per_connection"
            )
        ]

    def __str__(self):
        return f"{self.connection.name}: {self.path or self.name}"

    @property
    def label(self) -> str:
        return self.path or self.name


class Team(models.Model):
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    groups = models.ManyToManyField(
        ProviderGroup,
        blank=True,
        related_name="teams",
        help_text=(
            "Members join these groups in every app where their access profile gives them "
            "an account."
        ),
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AccessProfile(models.Model):
    """The accounts and groups a kind of person gets, e.g. "Staff" or "Volunteer"."""

    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    # Person types that get this profile when added without one.
    default_for = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @classmethod
    def default_for_type(cls, person_type: str) -> AccessProfile | None:
        for profile in cls.objects.all():
            if person_type in (profile.default_for or []):
                return profile
        return None


class ProfileGrant(models.Model):
    """An account at one connection, with groups, that an access profile gives."""

    profile = models.ForeignKey(AccessProfile, on_delete=models.CASCADE, related_name="grants")
    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name="grants")
    groups = models.ManyToManyField(ProviderGroup, blank=True, related_name="grants")

    class Meta:
        ordering = ["connection__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "connection"], name="unique_grant_per_profile_connection"
            )
        ]

    def __str__(self):
        return f"{self.profile}: {self.connection}"


class Person(models.Model):
    """A person whose access this app manages."""

    class Type(models.TextChoices):
        EMPLOYEE = "employee", "Employee"
        INTERN = "intern", "Intern"
        FREELANCER = "freelancer", "Freelancer"
        VOLUNTEER = "volunteer", "Volunteer"
        EXTERNAL = "external", "External"

    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        ONBOARDING = "onboarding", "Onboarding"
        ACTIVE = "active", "Active"
        OFFBOARDING = "offboarding", "Offboarding"
        OFFBOARDED = "offboarded", "Offboarded"
        ARCHIVED = "archived", "Archived"

    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    work_email = models.EmailField(unique=True, null=True, blank=True)
    personal_email = models.EmailField(blank=True)
    username = models.CharField(
        max_length=150,
        blank=True,
        help_text="Login name in connected apps. Empty: the part of the work email before @.",
    )
    job_title = models.CharField(max_length=200, blank=True)
    person_type = models.CharField(max_length=20, choices=Type.choices, default=Type.EMPLOYEE)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNED)
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="members"
    )
    manager = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reports"
    )
    access_profile = models.ForeignKey(
        AccessProfile, null=True, blank=True, on_delete=models.PROTECT, related_name="people"
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True, help_text="Last working day.")
    source_connection = models.ForeignKey(
        Connection,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sourced_people",
        help_text="The HR system this person was imported from.",
    )
    source_id = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_connection", "source_id"],
                condition=~Q(source_id=""),
                name="unique_source_record",
            )
        ]
        indexes = [models.Index(fields=["status"]), models.Index(fields=["start_date"])]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def login_name(self) -> str:
        if self.username:
            return self.username
        if self.work_email:
            return self.work_email.split("@", 1)[0].lower()
        return ""

    @property
    def has_access(self) -> bool:
        return self.status in {
            self.Status.ONBOARDING,
            self.Status.ACTIVE,
        }


class Account(models.Model):
    """An account in a connected app, linked to a person or not (yet)."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DISABLED = "disabled", "Disabled"
        MISSING = "missing", "Not found"
        UNKNOWN = "unknown", "Unknown"

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name="accounts")
    person = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="accounts"
    )
    external_id = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=255, blank=True)
    email = models.CharField(max_length=255, blank=True)
    display_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNKNOWN)
    # Group external IDs seen at the last observation; null when not observed.
    observed_groups = models.JSONField(null=True, blank=True)
    # Groups this app put the account in. They stay managed for this account
    # after a profile or team stops using them, so an update can remove them.
    applied_groups = models.JSONField(default=list, blank=True)
    observed_at = models.DateTimeField(null=True, blank=True)
    created_by_app = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["connection__name", "username"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "external_id"],
                condition=~Q(external_id=""),
                name="unique_account_external_id",
            ),
            models.UniqueConstraint(
                fields=["connection", "person"],
                condition=Q(person__isnull=False),
                name="unique_account_per_person",
            ),
        ]

    def __str__(self):
        return f"{self.connection.name}: {self.username or self.email or self.external_id}"


class Workflow(models.Model):
    """One lifecycle change for one person: the jobs that carry out a
    desired-access snapshot."""

    class Kind(models.TextChoices):
        ONBOARDING = "onboarding", "Onboarding"
        ACCESS_UPDATE = "access_update", "Access update"
        OFFBOARDING = "offboarding", "Offboarding"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        WAITING = "waiting", "Waiting for people"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Needs attention"
        CANCELLED = "cancelled", "Cancelled"

    OPEN_STATUSES = (Status.RUNNING, Status.WAITING, Status.FAILED)

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="workflows")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    desired = models.JSONField(default=dict)
    automatic = models.BooleanField(default=False)
    reason = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return f"{self.get_kind_display()} of {self.person}"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES


class Job(models.Model):
    """One change at one connection, run by the worker or handed to a person."""

    class Action(models.TextChoices):
        ENSURE_ACCOUNT = "ensure_account", "Create or update account"
        DISABLE_ACCOUNT = "disable_account", "Disable account"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        HELD = "held", "Waiting for approval"
        MANUAL = "manual", "Manual task open"
        FAILED = "failed", "Failed"
        SUCCEEDED = "succeeded", "Done"
        CANCELLED = "cancelled", "Cancelled"

    OPEN_STATUSES = (Status.QUEUED, Status.RUNNING, Status.HELD, Status.MANUAL, Status.FAILED)

    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="jobs")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="jobs")
    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name="jobs")
    action = models.CharField(max_length=30, choices=Action.choices)
    groups = models.JSONField(default=list, blank=True)
    managed_groups = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)
    # Set by each claim: only the worker holding the latest one settles the job.
    claim = models.UUIDField(null=True, blank=True, editable=False)
    last_error = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "connection__name"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]

    def __str__(self):
        return f"{self.get_action_display()} at {self.connection} for {self.person}"

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES


class Task(models.Model):
    """Work a person does by hand: a change in an app without automation, or a
    job that needs help."""

    class Kind(models.TextChoices):
        CHANGE = "change", "Make a change"
        FAILURE = "failure", "Fix a failed change"
        CREDENTIALS = "credentials", "Hand over sign-in details"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Cancelled"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    title = models.CharField(max_length=300)
    instructions = models.TextField(blank=True)
    person = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks"
    )
    job = models.ForeignKey(
        Job, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks"
    )
    connection = models.ForeignKey(
        Connection, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status"])]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "kind"],
                condition=Q(status="open", job__isnull=False),
                name="one_open_task_per_job_and_kind",
            )
        ]

    def __str__(self):
        return self.title


class SyncRun(models.Model):
    """A read from a connection: group import, account comparison, HR import."""

    class Kind(models.TextChoices):
        GROUPS = "groups", "Group import"
        ACCOUNTS = "accounts", "Account comparison"
        PEOPLE = "people", "People import"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name="sync_runs")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    summary = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.get_kind_display()} from {self.connection}"


class AuditEvent(models.Model):
    """Append-only record of who changed what. Never holds secret values."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    actor_label = models.CharField(max_length=255)
    action = models.CharField(max_length=100)
    summary = models.CharField(max_length=500)
    person = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events"
    )
    target_type = models.CharField(max_length=50, blank=True)
    target_id = models.CharField(max_length=50, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.summary


class WorkerHeartbeat(models.Model):
    """When the worker last finished a loop and each scheduled duty last ran."""

    name = models.CharField(max_length=50, primary_key=True)
    beat_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.name
