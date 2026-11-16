"""Create the first admin. The person signs in with this email address; the
first sign-in with a verified address links the provider's account."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email

from iam import audit
from iam.models import Role, User


class Command(BaseCommand):
    help = "Create or reactivate an admin app user by email."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--name", default="")

    def handle(self, *args, email, name, **options):
        email = email.strip().lower()
        try:
            validate_email(email)
        except ValidationError as exc:
            raise CommandError("Enter a valid email address.") from exc
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            user = User.objects.create_user(email=email, role=Role.ADMIN, display_name=name)
            verb = "Created"
        else:
            user.role = Role.ADMIN
            user.role_source = User.RoleSource.LOCAL
            user.is_active = True
            user.save(update_fields=["role", "role_source", "is_active"])
            verb = "Updated"
        audit.record("user.saved", f"{verb} admin {email} from the command line", target=user)
        self.stdout.write(f"{verb} admin {email}. Sign in with this address.")
