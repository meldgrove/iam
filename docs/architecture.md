# How it works

## Desired access

A person's **access profile** grants an account at each of its connections,
with groups. The person's **team** adds the team's groups, but only at
connections where the profile grants an account. People who are planned,
offboarding, offboarded or archived should have no access at all.

**Managed groups** are all groups that any access profile or team uses. The
app adds and removes only managed groups; memberships it does not manage
stay as they are in the connected app.

The person page shows, per app, what the person should have (and whether a
group comes from the profile or the team) next to the account the app last
saw.

## Workflows and jobs

Starting a workflow captures the person's desired access and plans one job
per connection:

| Workflow | Starts | Person's status | Jobs |
| --- | --- | --- | --- |
| Onboarding | By an approver, or by the schedule a set number of days before the start date | Planned → Onboarding → Active | Create or update the account where the profile grants one |
| Access update | By an approver after changing profile, team or policy | stays Active | Create or update where granted; disable known accounts no longer granted |
| Offboarding | By an approver, or by the schedule the day after the last day | → Offboarding → Offboarded | Disable every known account and every account the profile grants |

A new workflow for the same person replaces the open one: its unfinished jobs
are cancelled. Onboarding and access updates can be cancelled; offboarding
cannot (retry or complete its steps instead). Offboarding turns off the
person's app user at once.

**Jobs.** The worker (`manage.py run_worker`) runs queued jobs. For a
connection in automatic mode it calls the adapter:

- *Create or update account*: find the account (by its saved ID, then by
  username or email), create it if missing, reactivate it if disabled, then
  add the wanted managed groups and remove the managed groups no longer
  wanted.
- *Disable account*: remove the managed groups (unless the adapter keeps
  them, e.g. Relution) and disable the account. No account found counts as
  done.

The operations are idempotent, so a job can safely run again.

**Failures.** Timeouts, rate limits and server errors retry after 1, 2, 4,
8 and 16 minutes; after the sixth attempt, or at once for any other error,
the job fails and a failure task asks a person to fix the cause and press
Retry, or to make the change by hand and complete the task. Error messages
never contain credentials or provider responses.

Groups never hold up onboarding or offboarding. When a group step fails, the
account is still created, reactivated or disabled, and a new account's
password is still sent. Passing errors run the job again for the groups.
Once retries run out, or at once for any other error, the job succeeds and a
failure task names the groups to fix by hand. The next workflow for the
person cancels that task, because it plans the groups again.

**Manual tasks.** A connection in manual mode, a read-only or turned-off
connection, and apps without write support (Slack, Notion, other apps) get a
task with step-by-step instructions instead of an API call. Completing the
task records the change; cancelling it drops the change. Offboarding never
skips an app silently.

**Removal limit.** Automatic removals (offboarding started by the schedule)
count per connection over 24 hours. Beyond the limit in Admin › Settings, a
removal waits as "Waiting for approval" until an admin approves or rejects
it. Removals an approver starts are never held.

**Sign-in details.** When an adapter returns an initial password for a new
account (Google Workspace, or Keycloak with temporary passwords), the worker
emails it once to the person's personal address through the email
connection and forgets it. Without an email connection or personal address,
a task asks a person to set a new temporary password and hand it over.
Keycloak can instead send its own set-password email.

## Reads from connections

None of these change anything in a connected app.

- **Group import** reads the app's groups (roles, channels, teams) into the
  catalog that profiles and teams choose from. Groups the app no longer lists
  stay, marked missing, so the policy shows what broke.
- **Account comparison** reads all accounts, links unlinked accounts to people
  by work email or username (only on a unique match), marks accounts it no
  longer sees as missing, and reports drift: people who should have an
  account but have none, and offboarded people with active accounts. Fixing
  drift is an access update or offboarding an approver starts.
- **People import** (Personio) creates planned people and updates names,
  type, team, manager and dates. It never changes a work email, never
  offboards anyone by itself (the last day does that through the schedule),
  and reports people the HR system no longer lists.

The worker runs comparisons and imports on each connection's interval.

## Schedule

Once a minute the worker:

1. starts onboarding for planned people with an access profile whose start
   date is within the lead time (Admin › Settings);
2. starts offboarding for people whose last day has passed;
3. runs the people imports and account comparisons that are due.

Both automatic starts can be turned off in Admin › Settings.

## Audit log

Every change by a person or the worker writes an audit event: who, what, and
for whom. Connection changes record which settings changed, never their
values.

## Adapters

Each connected app is an adapter in `iam/adapters/`. Adapters get their
configuration as a dict, never touch the database, and return plain data;
the engine (`iam/jobs.py`, `iam/sync.py`) records the results. See
"Writing a new adapter" in [connections.md](connections.md).
