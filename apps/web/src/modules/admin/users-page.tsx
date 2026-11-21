import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@meldgrove/ui/badge";
import { Button } from "@meldgrove/ui/button";
import { Card, CardContent } from "@meldgrove/ui/card";
import { Page, PageHeader } from "@meldgrove/ui/page";
import { ErrorState, LoadingState } from "@meldgrove/ui/states";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@meldgrove/ui/table";
import { ROLE_LABELS, ROLE_SOURCE_LABELS } from "@iam/shared/labels";
import { useFormatter } from "@/lib/format";
import { Empty, TextLink } from "@/components/lists";
import { ToneBadge } from "@/components/status";
import { usersQuery } from "./api";

/** Who may use this app, with which role. */
export function UsersPage() {
  const format = useFormatter();
  const query = useQuery(usersQuery);
  return (
    <Page>
      <PageHeader
        title="Users and roles"
        description="Who may use this app. Viewers read; approvers manage people, workflows and tasks; admins also manage policy, connections, users and settings."
        actions={
          <Button asChild>
            <Link to="/admin/users/new">Add user</Link>
          </Button>
        }
      />
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <Card>
          <CardContent>
            {query.data.length === 0 ? (
              <Empty>No users yet.</Empty>
            ) : (
              <Table scrollLabel="Users">
                <TableHeader>
                  <TableRow>
                    <TableHead>Email</TableHead>
                    <TableHead>Name</TableHead>
                    <TableHead>Role</TableHead>
                    <TableHead>Role from</TableHead>
                    <TableHead>Person</TableHead>
                    <TableHead>Signed in</TableHead>
                    <TableHead>State</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {query.data.map((user) => (
                    <TableRow key={user.id}>
                      <TableCell>
                        <TextLink to={`/admin/users/${user.id}`}>{user.email}</TextLink>
                      </TableCell>
                      <TableCell>{user.displayName || "—"}</TableCell>
                      <TableCell>{ROLE_LABELS[user.role]}</TableCell>
                      <TableCell className="text-sm">{ROLE_SOURCE_LABELS[user.roleSource]}</TableCell>
                      <TableCell>
                        {user.person ? (
                          <TextLink to={`/people/${user.person.id}`}>{user.person.name}</TextLink>
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell className="text-muted-foreground text-sm whitespace-nowrap">
                        {user.lastLogin ? format.dateTime(user.lastLogin) : "never"}
                        {user.linked ? null : <div className="text-xs">Not linked yet</div>}
                      </TableCell>
                      <TableCell>
                        {user.isActive ? (
                          <ToneBadge tone="ok">Active</ToneBadge>
                        ) : (
                          <Badge variant="outline">Off</Badge>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      )}
    </Page>
  );
}
