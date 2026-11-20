import { useState, type FormEvent } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@meldgrove/ui/card";
import { Checkbox } from "@meldgrove/ui/checkbox";
import { FieldDescription, FieldError, FieldLegend, FieldSet } from "@meldgrove/ui/field";
import { Label } from "@meldgrove/ui/label";
import { Page, PageHeader } from "@meldgrove/ui/page";
import { ErrorState, LoadingState } from "@meldgrove/ui/states";
import {
  PERSON_TYPES,
  profileInputSchema,
  type ActionResult,
  type GroupConnection,
  type ProfileDetail,
  type ProfileInput,
} from "@iam/shared/api/iam";
import { PERSON_TYPE_LABELS, type PersonType } from "@iam/shared/labels";
import { api, ApiError, errorMessage } from "@/lib/api";
import { otherErrors, serverFieldErrors, validate, type FieldErrors } from "@/lib/form";
import { useRecordTitle } from "@/app/record-name";
import { TextLink } from "@/components/lists";
import { CheckboxField, FormActions, FormErrors, TextareaField, TextField } from "@/components/form-fields";
import { GroupChecklist } from "./group-picker";
import { groupConnectionsQuery, profileQuery, refreshPolicy } from "./api";

/** Add an access profile (`id` undefined) or edit one: name, defaults, and per connection an account and its groups. */
export function ProfileFormPage({ id }: { id?: number }) {
  const profile = useQuery({ ...profileQuery(id ?? 0), enabled: id !== undefined });
  const connections = useQuery(groupConnectionsQuery);
  useRecordTitle(`/profiles/${id}`, id === undefined ? undefined : profile.data?.name);
  const failed = (id !== undefined && profile.isError ? profile : null) ?? (connections.isError ? connections : null);
  if (failed) return <ErrorState error={failed.error} onRetry={() => void failed.refetch()} />;
  if ((id !== undefined && profile.isPending) || connections.isPending) return <LoadingState />;
  return <ProfileForm profile={id === undefined ? null : (profile.data ?? null)} connections={connections.data} />;
}

/** The groups chosen per connection that grants an account. */
type Grants = ReadonlyMap<number, ReadonlySet<number>>;

function ProfileForm({ profile, connections }: { profile: ProfileDetail | null; connections: GroupConnection[] }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [name, setName] = useState(profile?.name ?? "");
  const [description, setDescription] = useState(profile?.description ?? "");
  const [defaultFor, setDefaultFor] = useState<PersonType[]>(profile?.defaultFor ?? []);
  const [grants, setGrants] = useState<Grants>(
    () => new Map(profile?.grants.map((grant) => [grant.connection.id, new Set(grant.groups.map((group) => group.id))])),
  );
  const [errors, setErrors] = useState<FieldErrors>({});

  const save = useMutation({
    mutationFn: (input: ProfileInput) =>
      profile ? api.put<ActionResult>(`/profiles/${profile.id}`, input) : api.post<ActionResult>("/profiles", input),
    onSuccess: async (result) => {
      toast.success(result.message);
      await navigate({ to: `/profiles/${result.id ?? profile?.id}` });
      await refreshPolicy(client);
    },
    onError: (error) => {
      setErrors(serverFieldErrors(error));
      if (!(error instanceof ApiError && error.status === 422)) toast.error(errorMessage(error));
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    // Per granted connection, the groups the form shows: one that is no longer listed in its app drops out.
    const input: ProfileInput = {
      name,
      description,
      defaultFor: PERSON_TYPES.filter((type) => defaultFor.includes(type)),
      grants: connections
        .filter((connection) => grants.has(connection.id))
        .map((connection) => ({
          connectionId: connection.id,
          groupIds: connection.groups.map((group) => group.id).filter((groupId) => grants.get(connection.id)!.has(groupId)),
        })),
    };
    const invalid = validate(profileInputSchema, input);
    setErrors(invalid ?? {});
    if (!invalid) save.mutate(input);
  };

  const setGranted = (connectionId: number, granted: boolean) =>
    setGrants((current) => {
      const next = new Map(current);
      if (granted) next.set(connectionId, current.get(connectionId) ?? new Set());
      else next.delete(connectionId);
      return next;
    });

  // Choosing a group grants the account too.
  const toggleGroup = (connectionId: number, groupId: number, checked: boolean) =>
    setGrants((current) => {
      const groups = new Set(current.get(connectionId) ?? []);
      if (checked) groups.add(groupId);
      else groups.delete(groupId);
      return new Map(current).set(connectionId, groups);
    });

  const title = profile ? `Edit ${profile.name}` : "Add access profile";
  return (
    <Page className="max-w-4xl">
      <PageHeader title={title} description="Changes apply to people when an access update, onboarding or offboarding runs." />
      <form onSubmit={submit} noValidate className="grid gap-4">
        <Card>
          <CardContent className="grid gap-5">
            <TextField id="name" label="Name" value={name} onChange={setName} error={errors.name} maxLength={200} required />
            <TextareaField
              id="description"
              label="Description"
              value={description}
              onChange={setDescription}
              error={errors.description}
              rows={3}
            />
            <FieldSet data-invalid={errors.defaultFor ? true : undefined} aria-describedby="default-for-hint">
              <FieldLegend variant="label" className="mb-0">
                Default for
              </FieldLegend>
              <div className="flex flex-wrap gap-x-6 gap-y-2">
                {PERSON_TYPES.map((type) => (
                  <CheckboxField
                    key={type}
                    id={`default-for-${type}`}
                    label={PERSON_TYPE_LABELS[type]}
                    checked={defaultFor.includes(type)}
                    onChange={(checked) =>
                      setDefaultFor((current) => (checked ? [...current, type] : current.filter((item) => item !== type)))
                    }
                  />
                ))}
              </div>
              <FieldDescription id="default-for-hint" className="-mt-3">
                People of these types get this profile when they are added without one.
              </FieldDescription>
              {errors.defaultFor ? <FieldError className="-mt-4">{errors.defaultFor}</FieldError> : null}
            </FieldSet>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Accounts</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3">
            {connections.length === 0 ? (
              <p className="text-muted-foreground text-sm">
                No account connections yet. Add one in <TextLink to="/admin/connections">Admin › Connections</TextLink>.
              </p>
            ) : null}
            {connections.map((connection) => {
              const selected = grants.get(connection.id);
              return (
                <fieldset key={connection.id} className="grid gap-3 rounded-lg border p-4">
                  <legend className="sr-only">{connection.name}</legend>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <Checkbox
                        id={`grant-${connection.id}`}
                        checked={selected !== undefined}
                        onCheckedChange={(checked) => setGranted(connection.id, checked === true)}
                      />
                      <Label htmlFor={`grant-${connection.id}`} className="font-semibold">
                        Account in {connection.name}
                      </Label>
                    </div>
                    {connection.enabled ? null : <span className="text-muted-foreground text-sm">turned off</span>}
                  </div>
                  {connection.supportsGroups ? (
                    <GroupChecklist
                      connection={connection}
                      selected={selected ?? NONE}
                      onToggle={(groupId, checked) => toggleGroup(connection.id, groupId, checked)}
                      idPrefix={`grant-${connection.id}-group`}
                      emptyText={`No ${connection.groupLabel.toLowerCase()} imported yet.`}
                    />
                  ) : null}
                </fieldset>
              );
            })}
          </CardContent>
        </Card>
        <FormErrors messages={otherErrors(errors, /^(name|description|defaultFor)(\.|$)/)} />
        <FormActions pending={save.isPending} cancelTo={profile ? `/profiles/${profile.id}` : "/profiles"} />
      </form>
    </Page>
  );
}

const NONE: ReadonlySet<number> = new Set();
