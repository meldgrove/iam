# Glossary

The domain language of Meldgrove IAM. [docs/architecture.md](docs/architecture.md)
explains how the pieces work together.

- **Person**: someone whose access the app manages. Has a type (employee,
  intern, freelancer, volunteer, external), a status (planned, onboarding,
  active, offboarding, offboarded, archived), a team, an access profile, a
  start date and a last day.
- **App user**: someone who may use this app, with a role (viewer, approver,
  admin). May be linked to a person; offboarding that person turns the app
  user off.
- **Connection**: one connected app or service, configured in Admin ›
  Connections: an adapter type, settings, encrypted secrets, a mode
  (automatic, manual, read only) and an on/off switch.
- **Adapter**: the code for one kind of connection (`iam/adapters/`). Three
  kinds: accounts and groups (provisioning), people import (HR source),
  email.
- **Group**: something access can grant inside a connected app: a Keycloak
  group, a Google Group, a HubSpot team, a Slack channel. Imported into the
  group catalog, or added by hand for apps without a group API.
- **Access profile**: the accounts, with groups, a kind of person gets. One
  profile can be the default for each person type.
- **Team**: a group of people; its groups apply at every connection where the
  member's profile grants an account.
- **Desired access**: what profile and team grant, for people whose status
  allows access; nothing for everyone else.
- **Managed group**: a group any profile or team uses. The app adds and
  removes only managed groups.
- **Account**: an account in a connected app, linked to a person or not, with
  the status and groups the app last saw.
- **Workflow**: onboarding, access update or offboarding of one person. It
  keeps the desired-access snapshot it was started with.
- **Job**: one change at one connection: create or update an account, or
  disable it. Run by the worker, retried on passing errors.
- **Task**: work for a person: a change in an app without automation, a failed
  job, groups a job could not update, or sign-in details to hand over.
- **Held removal**: an automatic removal over the daily removal limit; it
  waits for an admin's approval.
- **Sync run**: a read from a connection: group import, account comparison
  or people import. Never changes the connected app.
- **Drift**: differences an account comparison finds between desired access
  and the accounts it saw.
- **Audit event**: an append-only record of a change, without secret values.
