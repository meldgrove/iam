import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@meldgrove/ui/button";
import { Card, CardContent } from "@meldgrove/ui/card";
import { Page, PageHeader } from "@meldgrove/ui/page";
import { ErrorState, LoadingState } from "@meldgrove/ui/states";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@meldgrove/ui/table";
import { useMe } from "@/hooks/use-me";
import { Empty, TextLink } from "@/components/lists";
import { truncate } from "@/lib/text";
import { profilesQuery } from "./api";

/** Access profiles with their people count, accounts and groups. */
export function ProfilesPage() {
  const me = useMe();
  const query = useQuery(profilesQuery);
  return (
    <Page>
      <PageHeader
        title="Access profiles"
        description="Each profile lists the apps a kind of person gets an account in, and the groups there."
        actions={
          me.isAdmin ? (
            <Button asChild>
              <Link to="/profiles/new">Add profile</Link>
            </Button>
          ) : null
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
              <Empty>No access profiles yet.</Empty>
            ) : (
              <Table scrollLabel="Access profiles">
                <TableHeader>
                  <TableRow>
                    <TableHead>Profile</TableHead>
                    <TableHead>People</TableHead>
                    <TableHead>Accounts and groups</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {query.data.map((profile) => (
                    <TableRow key={profile.id}>
                      <TableCell className="whitespace-normal">
                        <TextLink to={`/profiles/${profile.id}`}>{profile.name}</TextLink>
                        {profile.description ? (
                          <div className="text-muted-foreground text-xs">
                            {truncate(profile.description, 100)}
                          </div>
                        ) : null}
                      </TableCell>
                      <TableCell className="tabular-nums">{profile.peopleCount}</TableCell>
                      <TableCell className="text-sm whitespace-normal">
                        {profile.grants.length === 0
                          ? "—"
                          : profile.grants.map((grant) => (
                              <div key={grant.connection}>
                                <strong className="font-medium">{grant.connection}</strong>
                                {grant.groups.length > 0 ? `: ${grant.groups.join(", ")}` : ""}
                              </div>
                            ))}
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
