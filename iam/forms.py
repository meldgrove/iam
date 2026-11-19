from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from .adapters.base import Field
from .crypto import SecretUnreadable
from .models import AccessProfile, AppSettings, Connection, Person, Team, User


class DateInput(forms.DateInput):
    input_type = "date"


class PersonForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = [
            "first_name",
            "last_name",
            "work_email",
            "personal_email",
            "username",
            "job_title",
            "person_type",
            "team",
            "manager",
            "access_profile",
            "start_date",
            "end_date",
            "notes",
        ]
        widgets = {
            "start_date": DateInput(format="%Y-%m-%d"),
            "end_date": DateInput(format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "access_profile": "Empty: the profile that is the default for the person type.",
            "personal_email": "Receives the initial password of new accounts.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        manager_choices = Person.objects.exclude(status=Person.Status.ARCHIVED)
        if self.instance.pk:
            manager_choices = manager_choices.exclude(pk=self.instance.pk)
        self.fields["manager"].queryset = manager_choices

    def clean_work_email(self):
        value = self.cleaned_data.get("work_email")
        return value.strip().lower() if value else None

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "The last day cannot be before the start date.")
        if not cleaned.get("access_profile") and cleaned.get("person_type"):
            cleaned["access_profile"] = AccessProfile.default_for_type(cleaned["person_type"])
        return cleaned


class TeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ["name", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class AccessProfileForm(forms.ModelForm):
    default_for = forms.MultipleChoiceField(
        choices=Person.Type.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Default for",
        help_text="People of these types get this profile when they are added without one.",
    )

    class Meta:
        model = AccessProfile
        fields = ["name", "description", "default_for"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def clean_default_for(self):
        types = self.cleaned_data.get("default_for") or []
        others = AccessProfile.objects.exclude(pk=self.instance.pk)
        for profile in others:
            overlap = set(types) & set(profile.default_for or [])
            if overlap:
                labels = ", ".join(Person.Type(t).label for t in sorted(overlap))
                raise ValidationError(f"{profile.name} is already the default for {labels}.")
        return types


class UserForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["email", "display_name", "role", "is_active", "person"]
        help_texts = {
            "email": "The address the sign-in provider confirms. The first sign-in links the account.",
            "person": "Offboarding this person also turns off their app access.",
        }

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class SettingsForm(forms.ModelForm):
    class Meta:
        model = AppSettings
        fields = [
            "organization_name",
            "automatic_onboarding",
            "onboarding_lead_days",
            "automatic_offboarding",
            "removal_limit_per_day",
            "notification_emails",
        ]
        widgets = {"notification_emails": forms.Textarea(attrs={"rows": 3})}


KEEP_SECRET_HELP = "Saved. Leave empty to keep it."


class SecretTextarea(forms.Textarea):
    def format_value(self, value):
        # Like PasswordInput, never echo a submitted secret on validation errors.
        return None


class ConnectionForm(forms.Form):
    """Generated from the adapter's fields. Secret values are write-only."""

    name = forms.CharField(max_length=100)
    enabled = forms.BooleanField(
        required=False,
        help_text=(
            "Turned off, the app changes nothing there: onboarding skips it, offboarding "
            "creates a task, and the schedule does not read it."
        ),
    )
    mode = forms.ChoiceField(choices=Connection.Mode.choices, initial=Connection.Mode.AUTOMATIC)

    def __init__(self, adapter_class, *args, instance: Connection | None = None, **kwargs):
        self.adapter_class = adapter_class
        self.instance = instance
        initial = kwargs.pop("initial", {}) or {}
        if instance is not None:
            initial.update(name=instance.name, enabled=instance.enabled, mode=instance.mode)
            initial.update(instance.settings)
        else:
            initial.setdefault("name", adapter_class.name)
            for spec in adapter_class.fields:
                if spec.default is not None:
                    initial.setdefault(spec.name, spec.default)
        super().__init__(*args, initial=initial, **kwargs)
        if not adapter_class.supports_write or adapter_class.kind != "provisioning":
            del self.fields["mode"]
        self.saved_secrets = self._read_saved_secrets()
        saved = set(self.saved_secrets)
        for spec in adapter_class.fields:
            self.fields[f"cfg_{spec.name}"] = self._build(spec, spec.name in saved)
            if spec.name in initial and not spec.secret:
                self.initial[f"cfg_{spec.name}"] = initial[spec.name]

    def _read_saved_secrets(self) -> dict:
        """The saved secrets. Secrets no configured key can read count as
        not saved, so the admin enters them again instead of getting an error
        page whose report would carry the new values."""
        if self.instance is None or not self.instance.secret_data:
            return {}
        try:
            return self.instance.secrets()
        except SecretUnreadable:
            return {}

    @staticmethod
    def _build(spec: Field, saved: bool) -> forms.Field:
        common = {"label": spec.label, "help_text": spec.help, "required": spec.required}
        if spec.kind == "bool":
            return forms.BooleanField(**{**common, "required": False})
        if spec.kind == "int":
            return forms.IntegerField(min_value=0, **common)
        if spec.kind == "choice":
            return forms.ChoiceField(choices=spec.choices, **common)
        if spec.kind == "textarea":
            return forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), **common)
        if spec.kind == "email":
            return forms.EmailField(**common)
        if spec.kind == "url":
            field = forms.CharField(
                validators=[URLValidator(schemes=["http", "https"])],
                widget=forms.URLInput(attrs={"placeholder": spec.placeholder}),
                **common,
            )
            return field
        if spec.secret:
            if saved:
                common["required"] = False
                common["help_text"] = " ".join(filter(None, [spec.help, KEEP_SECRET_HELP]))
            widget = (
                SecretTextarea(attrs={"rows": 4, "autocomplete": "off", "spellcheck": "false"})
                if spec.kind == "secret_text"
                else forms.PasswordInput(attrs={"autocomplete": "new-password"}, render_value=False)
            )
            return forms.CharField(widget=widget, strip=spec.kind != "secret_text", **common)
        return forms.CharField(
            widget=forms.TextInput(attrs={"placeholder": spec.placeholder}), **common
        )

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        others = Connection.objects.filter(name__iexact=name)
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise ValidationError("Another connection has this name.")
        return name

    def clean(self):
        cleaned = super().clean()
        # Security: a saved secret must not follow a changed URL or host,
        # or an admin session could send it to any server.
        moved = [
            spec.label
            for spec in self.adapter_class.fields
            if spec.destination
            and self.instance is not None
            and f"cfg_{spec.name}" in cleaned
            and self._effective(spec, cleaned[f"cfg_{spec.name}"])
            != self._effective(spec, self.instance.settings.get(spec.name))
        ]
        if moved and self.saved_secrets:  # only an existing connection has saved secrets
            for spec in self.adapter_class.fields:
                key = f"cfg_{spec.name}"
                if spec.name in self.saved_secrets and not cleaned.get(key) and key in cleaned:
                    self.add_error(
                        key, f"Enter it again: {', '.join(moved)} changed where it is sent."
                    )
        return cleaned

    @staticmethod
    def _effective(spec: Field, value):
        """The value the adapter works with: an empty setting means its default."""
        return (spec.default if value in (None, "") else value) or ""

    def config_fields(self):
        return [self[f"cfg_{spec.name}"] for spec in self.adapter_class.fields]

    def save(self) -> Connection:
        connection = self.instance or Connection(adapter=self.adapter_class.slug)
        connection.name = self.cleaned_data["name"]
        connection.enabled = self.cleaned_data["enabled"]
        connection.mode = self.cleaned_data.get("mode") or (
            Connection.Mode.AUTOMATIC
            if self.adapter_class.supports_write
            else Connection.Mode.MANUAL
        )
        values, secrets = {}, dict(self.saved_secrets)
        for spec in self.adapter_class.fields:
            value = self.cleaned_data.get(f"cfg_{spec.name}")
            if spec.secret:
                if value:
                    secrets[spec.name] = value
            else:
                values[spec.name] = value if value is not None else ""
        connection.settings = values
        connection.set_secrets(secrets)
        connection.status = Connection.Status.UNTESTED
        connection.status_message = ""
        connection.save()
        return connection

    def changed_setting_names(self) -> list[str]:
        """Names of changed settings for the audit log, never values."""
        names = []
        for spec in self.adapter_class.fields:
            key = f"cfg_{spec.name}"
            if spec.secret:
                if self.cleaned_data.get(key):
                    names.append(spec.name)
            elif key in self.changed_data:
                names.append(spec.name)
        return names + [n for n in ("name", "enabled", "mode") if n in self.changed_data]
