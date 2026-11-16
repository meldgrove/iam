"""Role checks for views. Viewers read; approvers manage people, workflows
and tasks; admins also manage policy, connections, users and settings."""

from functools import wraps

from django.core.exceptions import PermissionDenied

from .models import Role


def role_required(role: str):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated or not request.user.has_role(role):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


viewer_required = role_required(Role.VIEWER)
approver_required = role_required(Role.APPROVER)
admin_required = role_required(Role.ADMIN)
