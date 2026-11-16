"""Demo data for development and screenshots: a manual app, profiles, teams
and people. Refuses to run outside development."""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from iam.models import (
    AccessProfile,
    Connection,
    Person,
    ProfileGrant,
    ProviderGroup,
    Role,
    Team,
    User,
)


class Command(BaseCommand):
    help = "Create demo data (development only)."

    def add_arguments(self, parser):
        parser.add_argument("--admin-email", default="admin@example.invalid")

    def handle(self, *args, admin_email, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo runs only with DJANGO_DEBUG=1.")
        User.objects.get_or_create(
            email=admin_email, defaults={"role": Role.ADMIN, "display_name": "Demo Admin"}
        )
        figma, _ = Connection.objects.get_or_create(
            name="Figma",
            defaults={
                "adapter": "manual",
                "enabled": True,
                "mode": Connection.Mode.MANUAL,
                "settings": {"app_name": "Figma", "instructions": "Assign an editor seat."},
            },
        )
        design, _ = ProviderGroup.objects.get_or_create(
            connection=figma,
            external_id="manual:design",
            defaults={"name": "Design", "manual": True},
        )
        staff, _ = AccessProfile.objects.get_or_create(
            name="Staff",
            defaults={
                "description": "Employees and interns",
                "default_for": ["employee", "intern"],
            },
        )
        volunteers, _ = AccessProfile.objects.get_or_create(
            name="Volunteers", defaults={"description": "Volunteers", "default_for": ["volunteer"]}
        )
        grant, _ = ProfileGrant.objects.get_or_create(profile=staff, connection=figma)
        product, _ = Team.objects.get_or_create(
            name="Product", defaults={"description": "Product and design"}
        )
        product.groups.add(design)
        Team.objects.get_or_create(name="Operations")
        today = timezone.localdate()
        people = [
            ("Ada", "Lovelace", "employee", staff, product, today - timedelta(days=400), None),
            ("Grace", "Hopper", "employee", staff, product, today + timedelta(days=5), None),
            (
                "Alan",
                "Turing",
                "intern",
                staff,
                None,
                today - timedelta(days=60),
                today + timedelta(days=12),
            ),
            (
                "Katherine",
                "Johnson",
                "volunteer",
                volunteers,
                None,
                today + timedelta(days=20),
                None,
            ),
        ]
        for first, last, kind, profile, team, start, end in people:
            Person.objects.get_or_create(
                work_email=f"{first.lower()}.{last.lower()}@example.invalid",
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "person_type": kind,
                    "access_profile": profile,
                    "team": team,
                    "start_date": start,
                    "end_date": end,
                    "personal_email": f"{first.lower()}@personal.example.invalid",
                },
            )
        self.stdout.write("Demo data ready.")
