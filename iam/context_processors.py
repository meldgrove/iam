from django.conf import settings

from .models import AppSettings, Job, Task


def app(request):
    context = {
        "app_version": settings.APP_VERSION,
        "provider_name": settings.OIDC_PROVIDER_NAME,
    }
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return context
    from .auth import account_url

    context.update(
        organization_name=AppSettings.load().organization_name,
        nav_open_tasks=Task.objects.filter(status=Task.Status.OPEN).count(),
        nav_failed_jobs=Job.objects.filter(status=Job.Status.FAILED).count(),
        nav_held_jobs=Job.objects.filter(status=Job.Status.HELD).count(),
        account_url=account_url(),
    )
    return context
