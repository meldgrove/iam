from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .. import audit
from ..lifecycle import LifecycleError, archive_person, can_archive, can_start, start_workflow
from ..models import Connection, Job, Person, Team, Workflow
from ..permissions import approver_required, viewer_required
from ..policy import entitled_access
from .base import paginate


@viewer_required
def person_list(request):
    people = Person.objects.select_related("team", "access_profile")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    team = request.GET.get("team", "")
    if q:
        people = people.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(work_email__icontains=q)
            | Q(username__icontains=q)
        )
    if status:
        people = people.filter(status=status)
    elif not q:
        people = people.exclude(status=Person.Status.ARCHIVED)
    if team.isdigit():
        people = people.filter(team_id=team)
    return render(
        request,
        "iam/people/list.html",
        {
            "page": paginate(request, people),
            "q": q,
            "status": status,
            "team": team,
            "statuses": Person.Status.choices,
            "teams": Team.objects.all(),
        },
    )


@viewer_required
def person_detail(request, pk):
    person = get_object_or_404(
        Person.objects.select_related("team", "manager", "access_profile", "source_connection"),
        pk=pk,
    )
    accounts = {a.connection_id: a for a in person.accounts.select_related("connection")}
    rows = []
    entitled = entitled_access(person)
    connection_ids = set(entitled) | set(accounts)
    for connection in Connection.objects.filter(pk__in=connection_ids):
        grant = entitled.get(connection.pk)
        rows.append(
            {
                "connection": connection,
                "account": accounts.get(connection.pk),
                "grant": grant,
                "groups": [
                    {"label": g.label, "reasons": sorted(grant.reasons.get(g.external_id, []))}
                    for g in grant.groups.values()
                ]
                if grant
                else [],
            }
        )
    return render(
        request,
        "iam/people/detail.html",
        {
            "person": person,
            "access_rows": rows,
            "workflows": person.workflows.all()[:20],
            "open_jobs": person.jobs.filter(status__in=Job.OPEN_STATUSES).select_related(
                "connection"
            ),
            "tasks": person.tasks.select_related("connection")[:20],
            "events": person.audit_events.all()[:30],
            "can_onboard": can_start(person, Workflow.Kind.ONBOARDING),
            "can_update": can_start(person, Workflow.Kind.ACCESS_UPDATE),
            "can_offboard": can_start(person, Workflow.Kind.OFFBOARDING),
            "can_archive": can_archive(person),
        },
    )


@approver_required
def person_create(request):
    from ..forms import PersonForm

    form = PersonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        person = form.save()
        audit.record(
            "person.created",
            f"Added {person.full_name}",
            actor=request.user,
            person=person,
            target=person,
        )
        messages.success(request, f"Added {person.full_name}.")
        return redirect("person_detail", pk=person.pk)
    return render(request, "iam/people/form.html", {"form": form})


@approver_required
def person_edit(request, pk):
    from ..forms import PersonForm

    person = get_object_or_404(Person, pk=pk)
    form = PersonForm(request.POST or None, instance=person)
    if request.method == "POST" and form.is_valid():
        person = form.save()
        audit.record(
            "person.updated",
            f"Edited {person.full_name}",
            actor=request.user,
            person=person,
            target=person,
            details={"fields": sorted(form.changed_data)},
        )
        access_fields = {"access_profile", "team"} & set(form.changed_data)
        if access_fields and person.status in (Person.Status.ACTIVE, Person.Status.ONBOARDING):
            messages.info(
                request,
                "Access changed. Start an access update to apply it in the connected apps.",
            )
        messages.success(request, f"Saved {person.full_name}.")
        return redirect("person_detail", pk=person.pk)
    return render(request, "iam/people/form.html", {"form": form, "person": person})


@approver_required
@require_POST
def person_start(request, pk, kind):
    person = get_object_or_404(Person, pk=pk)
    if kind not in Workflow.Kind.values:
        return redirect("person_detail", pk=pk)
    try:
        workflow = start_workflow(
            person, kind, actor=request.user, reason=request.POST.get("reason", "").strip()
        )
    except LifecycleError as exc:
        messages.error(request, str(exc))
        return redirect("person_detail", pk=pk)
    messages.success(request, f"Started {workflow.get_kind_display().lower()}.")
    return redirect("workflow_detail", pk=workflow.pk)


@approver_required
@require_POST
def person_archive(request, pk):
    person = get_object_or_404(Person, pk=pk)
    try:
        archive_person(person, actor=request.user)
    except LifecycleError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Archived {person.full_name}.")
    return redirect("person_detail", pk=pk)
