import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Button } from "@meldgrove/ui/button";
import { Card, CardContent } from "@meldgrove/ui/card";
import { Page, PageHeader } from "@meldgrove/ui/page";
import { Spinner } from "@meldgrove/ui/spinner";
import { ErrorState, LoadingState } from "@meldgrove/ui/states";
import { UpdatesSettingsCard } from "@meldgrove/ui/updates";
import { settingsInputSchema, type Settings, type SettingsInput } from "@iam/shared/api/iam";
import { api, ApiError, errorMessage } from "@/lib/api";
import { otherErrors, serverFieldErrors, validate, type FieldErrors } from "@/lib/form";
import { CheckboxField, FormErrors, TextareaField, TextField } from "@/components/form-fields";
import { refresh, settingsQuery } from "./api";

/** Admin › Settings: lifecycle rules and notifications, then the updates of the app. */
export function SettingsPage() {
  const query = useQuery(settingsQuery);
  return (
    <Page className="max-w-4xl">
      <PageHeader
        title="Settings"
        description="Lifecycle rules and notifications. Sign-in and deployment settings are environment variables (docs/configuration.md)."
      />
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <SettingsForm settings={query.data} />
      )}
      {/* The app layout's AdminUpdates provides the update status for admins. */}
      <div id="updates" className="scroll-mt-20">
        <UpdatesSettingsCard />
      </div>
    </Page>
  );
}

/**
 * A whole number as Django's IntegerField reads it: an optional sign, digits
 * and an optional ".0…" ("7", "+7", "7.00"); anything else ("1e2", "7.5") is
 * a message.
 */
function wholeNumber(text: string): { value: number } | { error: string } {
  if (text.trim() === "") return { error: "This field is required." };
  const match = /^\s*([+-]?\d+)(?:\.0*)?\s*$/.exec(text);
  return match ? { value: Number(match[1]) } : { error: "Enter a whole number." };
}

function SettingsForm({ settings }: { settings: Settings }) {
  const client = useQueryClient();
  const [organizationName, setOrganizationName] = useState(settings.organizationName);
  const [automaticOnboarding, setAutomaticOnboarding] = useState(settings.automaticOnboarding);
  const [onboardingLeadDays, setOnboardingLeadDays] = useState(String(settings.onboardingLeadDays));
  const [automaticOffboarding, setAutomaticOffboarding] = useState(settings.automaticOffboarding);
  const [removalLimitPerDay, setRemovalLimitPerDay] = useState(String(settings.removalLimitPerDay));
  const [notificationEmails, setNotificationEmails] = useState(settings.notificationEmails);
  const [errors, setErrors] = useState<FieldErrors>({});

  const save = useMutation({
    mutationFn: (input: SettingsInput) => api.put<Settings & { message: string }>("/settings", input),
    onSuccess: async (result) => {
      toast.success(result.message);
      client.setQueryData(settingsQuery.queryKey, result);
      // The organization name is the app's name in the frame.
      await refresh(client, [["settings"], ["audit"], ["me"]]);
    },
    onError: (error) => {
      setErrors(serverFieldErrors(error));
      if (!(error instanceof ApiError && error.status === 422)) toast.error(errorMessage(error));
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const leadDays = wholeNumber(onboardingLeadDays);
    const removalLimit = wholeNumber(removalLimitPerDay);
    const input: SettingsInput = {
      organizationName,
      automaticOnboarding,
      onboardingLeadDays: "value" in leadDays ? leadDays.value : 0,
      automaticOffboarding,
      removalLimitPerDay: "value" in removalLimit ? removalLimit.value : 0,
      notificationEmails,
    };
    const invalid: FieldErrors = { ...validate(settingsInputSchema, input) };
    if ("error" in leadDays) invalid.onboardingLeadDays = leadDays.error;
    if ("error" in removalLimit) invalid.removalLimitPerDay = removalLimit.error;
    setErrors(invalid);
    if (Object.keys(invalid).length === 0) save.mutate(input);
  };

  return (
    <form onSubmit={submit} noValidate className="grid gap-4">
      <Card>
        <CardContent className="grid gap-5 sm:grid-cols-2">
          <div className="sm:col-span-2 sm:max-w-[calc(50%-0.625rem)]">
            <TextField
              id="organization-name"
              label="Organization name"
              value={organizationName}
              onChange={setOrganizationName}
              error={errors.organizationName}
              maxLength={200}
              required
            />
          </div>
          <CheckboxField
            id="automatic-onboarding"
            label="Automatic onboarding"
            hint="Start onboarding for planned people when their start date nears."
            checked={automaticOnboarding}
            onChange={setAutomaticOnboarding}
            error={errors.automaticOnboarding}
          />
          <TextField
            id="onboarding-lead-days"
            label="Onboarding lead days"
            type="number"
            min={0}
            step={1}
            hint="Days before the start date when onboarding starts automatically."
            value={onboardingLeadDays}
            onChange={setOnboardingLeadDays}
            error={errors.onboardingLeadDays}
            required
          />
          <CheckboxField
            id="automatic-offboarding"
            label="Automatic offboarding"
            hint="Start offboarding the day after a person's end date."
            checked={automaticOffboarding}
            onChange={setAutomaticOffboarding}
            error={errors.automaticOffboarding}
          />
          <TextField
            id="removal-limit-per-day"
            label="Removal limit per day"
            type="number"
            min={0}
            step={1}
            hint="Automatic account removals per connection and 24 hours. Removals beyond the limit wait for an admin's approval. 0 holds every automatic removal."
            value={removalLimitPerDay}
            onChange={setRemovalLimitPerDay}
            error={errors.removalLimitPerDay}
            required
          />
          <div className="sm:col-span-2">
            <TextareaField
              id="notification-emails"
              label="Notification emails"
              hint="Addresses that get an email for new manual tasks, one per line."
              value={notificationEmails}
              onChange={setNotificationEmails}
              error={errors.notificationEmails}
              rows={3}
            />
          </div>
        </CardContent>
      </Card>
      <FormErrors
        messages={otherErrors(
          errors,
          /^(organizationName|automaticOnboarding|onboardingLeadDays|automaticOffboarding|removalLimitPerDay|notificationEmails)$/,
        )}
      />
      <div>
        <Button type="submit" disabled={save.isPending}>
          {save.isPending ? <Spinner /> : null}
          Save
        </Button>
      </div>
    </form>
  );
}
