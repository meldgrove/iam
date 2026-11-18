import hmac
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_not_required
from django.core.paginator import Paginator
from django.db import connection as db_connection
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from ..models import AuditEvent, Connection, Job, Person, Task, User, WorkerHeartbeat, Workflow
from ..permissions import viewer_required


def paginate(request, queryset, per_page=50):
    return Paginator(queryset, per_page).get_page(request.GET.get("page"))


@login_not_required
def login_page(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(
        request,
        "iam/login.html",
        {
            "oidc_enabled": settings.OIDC_ENABLED,
            "dev_login": settings.DEV_LOGIN,
            "next": request.GET.get("next", ""),
        },
    )


@login_not_required
def login_denied(request):
    return render(request, "iam/login_denied.html", status=403)


@login_not_required
@require_http_methods(["GET", "POST"])
def dev_login(request):
    if not settings.DEV_LOGIN:
        raise Http404
    if request.method == "POST":
        user = authenticate(request, dev_email=request.POST.get("email", ""))
        if user is not None:
            login(request, user, backend="iam.auth.DevLoginBackend")
            return redirect("dashboard")
    users = User.objects.filter(is_active=True)
    return render(request, "iam/dev_login.html", {"users": users})


@login_not_required
def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


@login_not_required
def readyz(request):
    """Database and worker health. Details only with the health check token."""
    token = request.headers.get("X-Health-Token", "")
    # Security: compare in constant time, so response times tell nothing about the token.
    detailed = bool(settings.HEALTHCHECK_TOKEN) and hmac.compare_digest(
        token.encode(), settings.HEALTHCHECK_TOKEN.encode()
    )
    checks = {}
    try:
        with db_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001
        checks["database"] = "unavailable"
    beat = (
        WorkerHeartbeat.objects.filter(name="worker").first()
        if checks["database"] == "ok"
        else None
    )
    max_age = timedelta(seconds=settings.WORKER_HEARTBEAT_MAX_AGE_SECONDS)
    checks["worker"] = "ok" if beat and beat.beat_at > timezone.now() - max_age else "stale"
    healthy = all(value == "ok" for value in checks.values())
    body = {"status": "ok" if healthy else "unhealthy"}
    if detailed:
        body["checks"] = checks
        body["version"] = settings.APP_VERSION
        body["read_only"] = settings.READ_ONLY
    return JsonResponse(body, status=200 if healthy else 503)


@viewer_required
def dashboard(request):
    today = timezone.localdate()
    counts = {status: 0 for status in Person.Status.values}
    for row in Person.objects.values("status").order_by():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    context = {
        "people_counts": counts,
        "open_tasks": Task.objects.filter(status=Task.Status.OPEN).count(),
        "failed_jobs": Job.objects.filter(status=Job.Status.FAILED).count(),
        "held_jobs": Job.objects.filter(status=Job.Status.HELD).count(),
        "open_workflows": Workflow.objects.filter(status__in=Workflow.OPEN_STATUSES).count(),
        "connection_errors": Connection.objects.filter(
            enabled=True, status=Connection.Status.ERROR
        ),
        "connections": Connection.objects.count(),
        "starting": Person.objects.filter(
            start_date__gte=today, start_date__lte=today + timedelta(days=30)
        )
        .exclude(status__in=[Person.Status.OFFBOARDED, Person.Status.ARCHIVED])
        .order_by("start_date")[:10],
        "leaving": Person.objects.filter(
            end_date__gte=today - timedelta(days=1), end_date__lte=today + timedelta(days=30)
        )
        .exclude(status__in=[Person.Status.OFFBOARDED, Person.Status.ARCHIVED])
        .order_by("end_date")[:10],
        "recent_events": AuditEvent.objects.select_related("person")[:12],
        "worker_beat": WorkerHeartbeat.objects.filter(name="worker").first(),
        "worker_max_age": settings.WORKER_HEARTBEAT_MAX_AGE_SECONDS,
    }
    beat = context["worker_beat"]
    context["worker_stale"] = beat is None or beat.beat_at < timezone.now() - timedelta(
        seconds=settings.WORKER_HEARTBEAT_MAX_AGE_SECONDS
    )
    return render(request, "iam/dashboard.html", context)
