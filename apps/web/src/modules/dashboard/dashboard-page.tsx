import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Alert, AlertDescription } from "@meldgrove/ui/alert";
import { Button } from "@meldgrove/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@meldgrove/ui/card";
import { Page, PageHeader } from "@meldgrove/ui/page";
import { ErrorState, LoadingState } from "@meldgrove/ui/states";
import { Table, TableBody, TableCell, TableRow } from "@meldgrove/ui/table";
import { cn } from "@meldgrove/ui/utils";
import { PERSON_STATUS_LABELS } from "@iam/shared/labels";
import type { Dashboard, PersonDateRow } from "@iam/shared/api/iam";
import { api } from "@/lib/api";
import { useFormatter } from "@/lib/format";
import { useMe } from "@/hooks/use-me";
import { StatusBadge } from "@/components/status";
import { Empty, EventsTable, TextLink } from "@/components/lists";

export const dashboardQuery = { queryKey: ["dashboard"], queryFn: () => api.get<Dashboard>("/dashboard") };

function Stat({ to, value, label, alert }: { to: string; value: number; label: string; alert?: boolean }) {
  return (
    <Link
      to={to}
      className={cn(
        "bg-card hover:bg-accent/40 rounded-xl border p-4 transition-colors",
        alert && value > 0 ? "border-destructive/50" : "",
      )}
    >
      <div className={cn("text-2xl font-semibold tabular-nums", alert && value > 0 ? "text-destructive" : "")}>{value}</div>
      <div className="text-muted-foreground text-sm">{label}</div>
    </Link>
  );
}

function PeopleDates({ title, rows }: { title: string; rows: PersonDateRow[] }) {
  const format = useFormatter();
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <Empty>Nobody.</Empty>
        ) : (
          <Table>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.id}>
                  <TableCell>
                    <TextLink to={`/people/${row.id}`}>{row.name}</TextLink>
                  </TableCell>
                  <TableCell className="text-muted-foreground">{format.date(row.date)}</TableCell>
                  <TableCell>
                    <StatusBadge value={row.status} labels={PERSON_STATUS_LABELS} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

/** People, access changes and what needs attention. */
export function DashboardPage() {
  const me = useMe();
  const format = useFormatter();
  const query = useQuery(dashboardQuery);
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const data = query.data;
  return (
    <Page>
      <PageHeader
        title="Dashboard"
        description="People, access changes and what needs attention."
        actions={
          me.isApprover ? (
            <Button asChild>
              <Link to="/people/new">Add person</Link>
            </Button>
          ) : null
        }
      />
      {data.connections === 0 && me.isAdmin ? (
        <Alert>
          <AlertDescription>
            <span>
              No apps are connected yet. Start in <TextLink to="/admin/connections">Admin › Connections</TextLink>, then
              create access profiles.
            </span>
          </AlertDescription>
        </Alert>
      ) : null}
      {data.worker.stale ? (
        <Alert variant="destructive">
          <AlertDescription>
            The worker has not reported in {data.worker.maxAgeSeconds} seconds
            {data.worker.beatAt ? ` (last: ${format.dateTime(data.worker.beatAt)})` : ""}. Jobs and the schedule do not run
            without it.
          </AlertDescription>
        </Alert>
      ) : null}
      {data.connectionErrors.map((connection) => (
        <Alert key={connection.id} variant="destructive">
          <AlertDescription>
            <span>
              Connection{" "}
              {me.isAdmin ? <TextLink to={`/admin/connections/${connection.id}`}>{connection.name}</TextLink> : connection.name}{" "}
              has an error: {connection.message.length > 160 ? `${connection.message.slice(0, 159)}…` : connection.message}
            </span>
          </AlertDescription>
        </Alert>
      ))}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Stat to="/tasks" value={data.openTasks} label="Open tasks" alert />
        <Stat to="/jobs?status=failed" value={data.failedJobs} label="Failed jobs" alert />
        <Stat to="/jobs?status=held" value={data.heldJobs} label="Removals to approve" alert />
        <Stat to="/workflows" value={data.openWorkflows} label="Open workflows" />
        <Stat to="/people?status=active" value={data.peopleCounts.active} label="Active people" />
        <Stat to="/people?status=planned" value={data.peopleCounts.planned} label="Planned" />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <PeopleDates title="Starting in the next 30 days" rows={data.starting} />
        <PeopleDates title="Leaving in the next 30 days" rows={data.leaving} />
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Recent activity</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3">
          <EventsTable events={data.recentEvents} showPerson />
          <p className="text-sm">
            <TextLink to="/audit">Full audit log</TextLink>
          </p>
        </CardContent>
      </Card>
    </Page>
  );
}
