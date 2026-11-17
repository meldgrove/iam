"""Workflows, jobs, tasks and the audit log."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..jobs import ActionError, approve_job, cancel_task, complete_task, reject_job, retry_job
from ..lifecycle import LifecycleError, cancel_workflow
from ..models import AuditEvent, Connection, Job, Task, Workflow
from ..permissions import admin_required, approver_required, viewer_required
from .base import paginate


@viewer_required
def workflow_list(request):
    workflows = Workflow.objects.select_related("person", "created_by")
    status = request.GET.get("status", "open")
    if status == "open":
        workflows = workflows.filter(status__in=Workflow.OPEN_STATUSES)
    elif status in Workflow.Status.values:
        workflows = workflows.filter(status=status)
    return render(
        request,
        "iam/workflows/list.html",
        {
            "page": paginate(request, workflows),
            "status": status,
            "statuses": Workflow.Status.choices,
        },
    )


@viewer_required
def workflow_detail(request, pk):
    workflow = get_object_or_404(Workflow.objects.select_related("person", "created_by"), pk=pk)
    return render(
        request,
        "iam/workflows/detail.html",
        {
            "workflow": workflow,
            "jobs": workflow.jobs.select_related("connection").order_by("connection__name"),
            "tasks": Task.objects.filter(job__workflow=workflow).select_related("connection"),
        },
    )


@approver_required
@require_POST
def workflow_cancel(request, pk):
    workflow = get_object_or_404(Workflow, pk=pk)
    try:
        cancel_workflow(workflow, actor=request.user)
    except LifecycleError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Cancelled the workflow.")
    return redirect("workflow_detail", pk=pk)


@viewer_required
def job_list(request):
    jobs = Job.objects.select_related("person", "connection", "workflow")
    status = request.GET.get("status", "attention")
    if status == "attention":
        jobs = jobs.filter(status__in=[Job.Status.FAILED, Job.Status.HELD])
    elif status == "open":
        jobs = jobs.filter(status__in=Job.OPEN_STATUSES)
    elif status in Job.Status.values:
        jobs = jobs.filter(status=status)
    connection = request.GET.get("connection", "")
    if connection.isdecimal():
        jobs = jobs.filter(connection_id=connection)
    return render(
        request,
        "iam/jobs/list.html",
        {
            "page": paginate(request, jobs),
            "status": status,
            "statuses": Job.Status.choices,
            "connection": connection,
            "connections": Connection.objects.all(),
        },
    )


@viewer_required
def job_detail(request, pk):
    job = get_object_or_404(Job.objects.select_related("person", "connection", "workflow"), pk=pk)
    return render(
        request,
        "iam/jobs/detail.html",
        {
            "job": job,
            "tasks": job.tasks.all(),
            "events": AuditEvent.objects.filter(target_type="job", target_id=str(job.pk)),
        },
    )


def _job_action(request, pk, action, success):
    job = get_object_or_404(Job, pk=pk)
    try:
        action(job, actor=request.user)
    except ActionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, success)
    return redirect("job_detail", pk=pk)


@approver_required
@require_POST
def job_retry(request, pk):
    return _job_action(request, pk, retry_job, "The job runs again.")


@admin_required
@require_POST
def job_approve(request, pk):
    return _job_action(request, pk, approve_job, "Approved. The job runs now.")


@admin_required
@require_POST
def job_reject(request, pk):
    return _job_action(request, pk, reject_job, "Rejected. Nothing was changed.")


@viewer_required
def task_list(request):
    tasks = Task.objects.select_related("person", "connection", "job")
    status = request.GET.get("status", Task.Status.OPEN)
    if status in Task.Status.values:
        tasks = tasks.filter(status=status)
    return render(
        request,
        "iam/tasks/list.html",
        {"page": paginate(request, tasks), "status": status, "statuses": Task.Status.choices},
    )


@viewer_required
def task_detail(request, pk):
    task = get_object_or_404(
        Task.objects.select_related("person", "connection", "job", "completed_by"), pk=pk
    )
    return render(request, "iam/tasks/detail.html", {"task": task})


@approver_required
@require_POST
def task_complete(request, pk):
    task = get_object_or_404(Task, pk=pk)
    try:
        complete_task(task, actor=request.user, note=request.POST.get("note", "").strip())
    except ActionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Task completed.")
    return redirect("task_detail", pk=pk)


@approver_required
@require_POST
def task_cancel(request, pk):
    task = get_object_or_404(Task, pk=pk)
    try:
        cancel_task(task, actor=request.user, note=request.POST.get("note", "").strip())
    except ActionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Task cancelled.")
    return redirect("task_detail", pk=pk)


@viewer_required
def audit_list(request):
    events = AuditEvent.objects.select_related("person", "actor")
    q = request.GET.get("q", "").strip()
    if q:
        events = events.filter(summary__icontains=q)
    return render(request, "iam/audit/list.html", {"page": paginate(request, events, 100), "q": q})
