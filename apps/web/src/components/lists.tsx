import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { Pager } from "@meldgrove/ui/pager";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@meldgrove/ui/table";
import {
  JOB_ACTION_LABELS,
  JOB_STATUS_LABELS,
  TASK_KIND_LABELS,
  TASK_STATUS_LABELS,
  WORKFLOW_KIND_LABELS,
  WORKFLOW_STATUS_LABELS,
} from "@iam/shared/labels";
import type { EventRow, JobRow, Paged, TaskRow, WorkflowRow } from "@iam/shared/api/iam";
import { useFormatter } from "@/lib/format";
import { StatusBadge } from "./status";

/** The lists several pages share: audit events, tasks, jobs, workflows, and the pager of a paged list. */

/** A text for an empty list, as the Django version had ("None yet."). */
export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-muted-foreground py-2 text-sm">{children}</p>;
}

/** An internal link that looks like one. */
export function TextLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link to={to} className="font-medium underline-offset-4 hover:underline">
      {children}
    </Link>
  );
}

export function EventsTable({ events, showPerson = false }: { events: EventRow[]; showPerson?: boolean }) {
  const format = useFormatter();
  if (events.length === 0) return <Empty>Nothing yet.</Empty>;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>When</TableHead>
          <TableHead>Who</TableHead>
          <TableHead>What</TableHead>
          {showPerson ? <TableHead>Person</TableHead> : null}
        </TableRow>
      </TableHeader>
      <TableBody>
        {events.map((event) => (
          <TableRow key={event.id}>
            <TableCell className="text-muted-foreground whitespace-nowrap">{format.dateTime(event.createdAt)}</TableCell>
            <TableCell className="text-muted-foreground">{event.actorLabel}</TableCell>
            <TableCell className="whitespace-normal">{event.summary}</TableCell>
            {showPerson ? (
              <TableCell>{event.person ? <TextLink to={`/people/${event.person.id}`}>{event.person.name}</TextLink> : "—"}</TableCell>
            ) : null}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function TasksTable({ tasks, showPerson = true }: { tasks: TaskRow[]; showPerson?: boolean }) {
  const format = useFormatter();
  if (tasks.length === 0) return <Empty>No tasks.</Empty>;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Task</TableHead>
          {showPerson ? <TableHead>Person</TableHead> : null}
          <TableHead>Created</TableHead>
          <TableHead>Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {tasks.map((task) => (
          <TableRow key={task.id}>
            <TableCell className="whitespace-normal">
              <TextLink to={`/tasks/${task.id}`}>{task.title}</TextLink>
              <div className="text-muted-foreground text-xs">{TASK_KIND_LABELS[task.kind]}</div>
            </TableCell>
            {showPerson ? (
              <TableCell>{task.person ? <TextLink to={`/people/${task.person.id}`}>{task.person.name}</TextLink> : "—"}</TableCell>
            ) : null}
            <TableCell className="text-muted-foreground whitespace-nowrap">{format.dateTime(task.createdAt)}</TableCell>
            <TableCell>
              <StatusBadge value={task.status} labels={TASK_STATUS_LABELS} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/** `actions`: buttons at the end of each row (e.g. Retry), for the roles that may use them. */
export function JobsTable({
  jobs,
  showPerson = true,
  actions,
}: {
  jobs: JobRow[];
  showPerson?: boolean;
  actions?: (job: JobRow) => ReactNode;
}) {
  if (jobs.length === 0) return <Empty>No open jobs.</Empty>;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Change</TableHead>
          <TableHead>App</TableHead>
          {showPerson ? <TableHead>Person</TableHead> : null}
          <TableHead>Status</TableHead>
          {actions ? (
            <TableHead>
              <span className="sr-only">Actions</span>
            </TableHead>
          ) : null}
        </TableRow>
      </TableHeader>
      <TableBody>
        {jobs.map((job) => (
          <TableRow key={job.id}>
            <TableCell className="whitespace-normal">
              <TextLink to={`/jobs/${job.id}`}>{JOB_ACTION_LABELS[job.action]}</TextLink>
              {/* A finished job keeps the error of an earlier attempt; it no longer matters. */}
              {job.lastError && job.status !== "succeeded" && job.status !== "rehearsed" ? (
                <div className="text-muted-foreground line-clamp-2 text-xs">{job.lastError}</div>
              ) : null}
            </TableCell>
            <TableCell>{job.connection.name}</TableCell>
            {showPerson ? (
              <TableCell>
                <TextLink to={`/people/${job.person.id}`}>{job.person.name}</TextLink>
              </TableCell>
            ) : null}
            <TableCell>
              <StatusBadge value={job.status} labels={JOB_STATUS_LABELS} />
            </TableCell>
            {actions ? (
              <TableCell>
                <div className="flex justify-end gap-2">{actions(job)}</div>
              </TableCell>
            ) : null}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function WorkflowsTable({ workflows, showPerson = true }: { workflows: WorkflowRow[]; showPerson?: boolean }) {
  const format = useFormatter();
  if (workflows.length === 0) return <Empty>None yet.</Empty>;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Workflow</TableHead>
          {showPerson ? <TableHead>Person</TableHead> : null}
          <TableHead>Started</TableHead>
          <TableHead>Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {workflows.map((workflow) => (
          <TableRow key={workflow.id}>
            <TableCell className="whitespace-normal">
              <TextLink to={`/workflows/${workflow.id}`}>{WORKFLOW_KIND_LABELS[workflow.kind]}</TextLink>
              {workflow.reason ? <div className="text-muted-foreground text-xs">{workflow.reason}</div> : null}
            </TableCell>
            {showPerson ? (
              <TableCell>
                <TextLink to={`/people/${workflow.person.id}`}>{workflow.person.name}</TextLink>
              </TableCell>
            ) : null}
            <TableCell className="text-muted-foreground whitespace-nowrap">
              {format.dateTime(workflow.createdAt)}
              <div className="text-xs">{workflow.createdBy ?? "the schedule"}</div>
            </TableCell>
            <TableCell>
              <StatusBadge value={workflow.status} labels={WORKFLOW_STATUS_LABELS} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/** The pager under a paged list (pages of `pageSize`). */
export function PageControls<T>({
  data,
  pageSize = 50,
  onPage,
}: {
  data: Paged<T>;
  pageSize?: number;
  onPage: (page: number) => void;
}) {
  if (data.pages <= 1) return null;
  return <Pager page={data.page} pageSize={pageSize} total={data.total} onPage={onPage} />;
}
