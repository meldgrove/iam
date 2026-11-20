import { Link, useNavigate } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@meldgrove/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@meldgrove/ui/card";
import { Page, PageHeader } from "@meldgrove/ui/page";
import { ErrorState, LoadingState } from "@meldgrove/ui/states";
import { Table, TableBody, TableCell, TableRow } from "@meldgrove/ui/table";
import { PERSON_STATUS_LABELS } from "@iam/shared/labels";
import { useMe } from "@/hooks/use-me";
import { useRecordTitle } from "@/app/record-name";
import { Empty, TextLink } from "@/components/lists";
import { StatusBadge } from "@/components/status";
import { DeleteButton } from "@/components/delete-button";
import { refreshPolicy, teamQuery } from "./api";

/** A team: its groups and members; admins edit or delete it. */
export function TeamPage({ id }: { id: number }) {
  const me = useMe();
  const navigate = useNavigate();
  const client = useQueryClient();
  const query = useQuery(teamQuery(id));
  useRecordTitle(`/teams/${id}`, query.data?.name);
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const team = query.data;
  return (
    <Page>
      <PageHeader
        title={team.name}
        description={team.description || undefined}
        actions={
          me.isAdmin ? (
            <>
              <Button asChild variant="outline">
                <Link to={`/teams/${id}/edit`}>Edit</Link>
              </Button>
              <DeleteButton
                path={`/teams/${id}`}
                title={`Delete ${team.name}?`}
                description="Its members stay, without a team."
                onDeleted={async () => {
                  await navigate({ to: "/teams" });
                  client.removeQueries({ queryKey: ["teams", id] });
                  await refreshPolicy(client);
                }}
              />
            </>
          ) : null
        }
      />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Groups</CardTitle>
          </CardHeader>
          <CardContent>
            {team.groups.length === 0 ? (
              <Empty>None.</Empty>
            ) : (
              <ul className="grid gap-1 text-sm">
                {team.groups.map((group) => (
                  <li key={group.id}>
                    {group.connection}: {group.label}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Members</CardTitle>
          </CardHeader>
          <CardContent>
            {team.members.length === 0 ? (
              <Empty>No members.</Empty>
            ) : (
              <Table scrollLabel="Members">
                <TableBody>
                  {team.members.map((person) => (
                    <TableRow key={person.id}>
                      <TableCell>
                        <TextLink to={`/people/${person.id}`}>{person.name}</TextLink>
                      </TableCell>
                      <TableCell className="text-muted-foreground">{person.accessProfile ?? "—"}</TableCell>
                      <TableCell>
                        <StatusBadge value={person.status} labels={PERSON_STATUS_LABELS} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>
    </Page>
  );
}
