"""Any app without an API: every change becomes a task for a person."""

from __future__ import annotations

from .base import AccountSpec, Adapter, Field, TestResult


class ManualAdapter(Adapter):
    slug = "manual"
    name = "Other app (manual tasks)"
    description = (
        "Tracks accounts in an app without automation. Each account or group change "
        "becomes a task with the steps below; completing the task records the change."
    )
    supports_write = False
    supports_groups = True
    supports_account_listing = False
    manual_groups = True
    fields = (
        Field("app_name", "App name", required=True, placeholder="Figma"),
        Field("admin_url", "Admin page", kind="url", help="Linked in every task."),
        Field(
            "instructions",
            "Instructions",
            kind="textarea",
            help="Added to every task, e.g. which license to assign.",
        ),
    )

    @property
    def app_name(self) -> str:
        return self.setting("app_name", "the app")

    def test_connection(self) -> TestResult:
        return TestResult(True, "Manual connections need no test: changes become tasks.")

    def manual_instructions(self, action: str, spec: AccountSpec, groups: list[str]) -> str:
        who = f"{spec.display_name} ({spec.email or spec.username})"
        if action == "disable_account":
            lines = [f"Remove or disable the {self.app_name} account of {who}."]
        else:
            lines = [f"Make sure {who} has an active {self.app_name} account."]
            if groups:
                lines.append("Groups: " + ", ".join(groups) + ".")
        if self.setting("admin_url"):
            lines.append(f"Admin page: {self.setting('admin_url')}")
        if self.setting("instructions"):
            lines.append(self.setting("instructions"))
        return "\n".join(lines)
