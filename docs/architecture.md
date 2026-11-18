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
| Access update | By an approver after changing profile, team or policy; during onboarding it starts a new onboarding instead | stays Active | Create or update where granted; disable known accounts no longer granted |
| Offboarding | By an approver, or by the schedule the day after the last day | → Offboarding → Offboarded | Disable every known account (also one the last comparison no longer listed) and every account the profile grants |

A new workflow for the same person replaces the open one: its unfinished jobs
are cancelled. Onboarding and access updates can be cancelled; offboarding
cannot (retry or complete its steps instead). A cancelled onboarding returns
the person to planned, and the schedule does not start it again until the
start date or the access profile changes (an approver can still start it).
Onboarding refuses a last day that has passed: clear or change it first, or
the schedule would offboard the person right after. A job that is running
when its workflow ends finishes, and its result (the account, an initial
password) is kept. Offboarding turns off the person's app user at once.

**Jobs.** The worker (`manage.py run_worker`) runs queued jobs. For a
connection in automatic mode it calls the adapter:

- *Create or update account*: find the account (by its saved ID, then by
  username or email), create it if missing, reactivate it if disabled, then
  add the wanted managed groups and remove the managed groups no longer
  wanted.

  An account found without its saved ID must be the person's. A Keycloak
  username whose user has another email, or several SCIM users with that
  userName or email, stop the job with nothing changed. A Google address
  that is an alias of another user is not the person's account (creating
  it stops the job). The job then changes exactly the account whose owner
  it checked.
- *Disable account*: remove the managed groups (unless the adapter keeps
  them, e.g. Relution) and disable the account. No account found counts as
  done.

The operations are idempotent, so a job can safely run again.

**Failures.** A read (GET) that a rate limit, a 502, 503 or 504, or a
refused connection stops is tried twice more at once, after the pause the
connected app asks for (up to 30 seconds) or 1 and 4 seconds; changes are
never sent twice. Timeouts, rate limits and server errors that remain retry the job
after 1, 2, 4, 8 and 16 minutes; after the sixth attempt, or at once for any
other error, the job fails and a failure task asks a person to fix the cause
and press Retry, or to make the change by hand and complete the task. Error
messages never contain credentials or provider responses.

**A stopped worker.** A job whose worker stopped mid-run (a deploy, a crash,
a lost database) runs again once its lease (`JOB_LEASE_SECONDS`) runs out.
If the app had already created the account, the sign-in details that run
arranged are lost: when the next run finds an account the app does not know
as the person's, at an app that hands out sign-in details (Google Workspace,
Keycloak), a task asks a person to check that the person can sign in.

Groups never hold up onboarding or offboarding. When a group step fails, the
account is still created, reactivated or disabled, and a new account's
password is still sent. Passing errors run the job again for the groups; the
account change is audited before that. Once retries run out, or
at once for any other error, the job succeeds and a failure task names the
groups to fix by hand. A later workflow that plans a job at the same
connection cancels that task, because that job does the groups again.

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
That task stays open when its job is retried, cancelled or replaced: nothing
would create it again. Keycloak can instead send its own set-password email;
when it cannot send it (or set the temporary password), a task asks a person
to do that.

## Read-only mode

With `READ_ONLY=1` ([configuration.md](configuration.md)) the app can be
tried with real connected apps and real people without changing anything
in those apps:

- **Adapters can only read.** `Connection.build_adapter` hands out a
  wrapper that lets through only reads (tests, listings, finding an account
  and its groups) and refuses everything else, including operations added
  later, before it reaches the app.
- **No email** leaves the app: no initial passwords, no task notifications.
- **Jobs are rehearsed.** The worker finds the account and its groups as
  the job would, checks that the account is the person's, and records what
  it would change (create, reactivate or disable the account; the groups
  to add and remove). At connections without automation it records the
  steps a person would get, without a task. The job ends as "Checked only
  (read-only mode)". A read that fails is a failed job, as always.
- **Workflows complete** once their jobs are rehearsed, so people become
  active or offboarded in this app. The schedule, imports and account
  comparisons run as usual.

Turning the mode off replays nothing: an access update applies a person's
access for real. Jobs still queued then run for real, and so do held or
failed jobs that someone approves or retries. Every page after sign-in shows
a banner while the mode is on, and the detailed `/-/readyz` answer reports
it.

## Reads from connections

None of these change anything in a connected app.

- **Group import** reads the app's groups (roles, channels, teams) into the
  catalog that profiles and teams choose from. Groups the app no longer lists
  stay, marked missing, so the policy shows what broke. The app neither adds
  nor removes memberships of a missing group until an import lists it again:
  an answer that only seems empty must not strip people of their groups.
- **Account comparison** reads all accounts, links unlinked accounts to people
  by work email or username (only on a unique match), marks accounts it no
  longer sees as missing, and reports drift: people who should have an
  account but have none, and active accounts the person's access does not
  grant (including planned people, e.g. after a cancelled onboarding, and
  offboarded people). Fixing drift is an access update or offboarding an
  approver starts. A scheduled comparison whose listing lacks at least 10,
  and more than half, of the accounts it saw before fails and records
  nothing: such an answer is more likely broken than true. "Compare
  accounts now" records it.
- **People import** (Personio) creates planned people and updates names,
  type, team, manager and dates. It never changes a work email, never
  offboards anyone by itself (the last day does that through the schedule),
  and reports people the HR system no longer lists.

The worker runs comparisons and imports on each connection's interval.

## Schedule

Once a minute the worker:

1. starts onboarding for planned people with an access profile whose start
   date is within the lead time (Admin › Settings), unless an approver
   cancelled their last onboarding;
2. starts offboarding for people whose last day has passed (planned people
   only when they have an account or an app user);
3. completes open workflows whose jobs are all done;
4. after the queued jobs, runs the one people import or account comparison
   that is most overdue. A slow connection holds up the jobs for one read,
   and the jobs queued meanwhile run next. A read that failed comes again
   after 15 minutes, then 30, 60 and so on, never later than its interval.

Both automatic starts can be turned off in Admin › Settings. With several
workers, one at a time runs the schedule and one at a time reads from
connections (PostgreSQL advisory locks), so one worker can run jobs while
another reads, and a job's result counts only from the worker that holds
its current claim.

The worker writes a heartbeat each round and after each job, which the
dashboard and `/-/readyz` check. A watchdog thread ends a worker that made
no progress for `WORKER_STALL_SECONDS` (every answer from a connected app
counts), for example one stuck on a database connection whose server went
away. It logs where each thread was, and the container's restart policy
starts a fresh worker.

## Audit log

Every change by a person or the worker writes an audit event: who, what, and
for whom. Connection changes record which settings changed, never their
values.

## Adapters

Each connected app is an adapter in `iam/adapters/`. Adapters get their
configuration as a dict, never touch the database, and return plain data;
the engine (`iam/jobs.py`, `iam/sync.py`) records the results. See
"Writing a new adapter" in [connections.md](connections.md).
