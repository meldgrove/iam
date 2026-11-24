# Coolify

Meldgrove IAM is made for a Linux server ([deployment.md](deployment.md)). It
also runs on Coolify, from `compose.coolify.yaml`: the services of
`compose.yaml`, with `expose` instead of a host port, pulling the images at
every deploy, and with an `updater` that calls Coolify's deploy link. Health
checks, the first steps, read-only mode and backups are the same as on a
server.

## Setup

1. Create a PostgreSQL resource (16 or newer) and note its internal URL.
2. Create a Docker Compose resource from this repository, branch `stable`
   (it always holds the Compose files of the newest release), with the
   Docker Compose location `/compose.coolify.yaml`. Under Advanced, set Auto
   deploy to "Manual deployments only" so only the updater deploys. Turn on
   "Connect To Predefined Network" to reach the database.
3. Assign the domain to `web`, port 8000.
4. Set the environment variables from the table in
   [deployment.md](deployment.md#setup), step 3, with `DATABASE_URL` set to
   the PostgreSQL resource's internal URL and `IAM_VERSION` to a release
   series such as `0.3`, or `preview` (see
   [Automatic updates](#automatic-updates)). `UPDATER_TOKEN` is
   recommended ([below](#protect-the-update-service)).
5. Deploy. Sign in, then continue with the
   [first steps](deployment.md#first-steps-after-the-first-sign-in).
6. **Automatic updates:** set them up [below](#automatic-updates).

## Settings from the command line

The operator commands
([deployment.md](deployment.md#settings-from-the-command-line)) run in a
terminal of `web` (the resource's Terminal page): `cli settings list`.
`cli settings apply <name>` there asks for the body and does not show it:
paste it, then press Ctrl-D. Do not type the body into the shell itself, for
example as a heredoc: the shell's history would keep the secret. From
the server's shell, `docker ps` names the `web` container
(`web-<resource UUID>-…`); `docker exec -i <container> cli settings apply
connections < body.json` pipes a body into it.

## Upgrades

Releases that need operator action list their steps under "Upgrade steps
needed" in the release notes ([releases.md](releases.md)). Take the steps,
then change `IAM_VERSION` in the resource's environment variables and
redeploy. Coolify reads `compose.coolify.yaml` from `stable`, which is at the
newest release.

### From the Django version (0.1, 0.2)

1. Back up the PostgreSQL resource.
2. Keep the environment variables: `compose.coolify.yaml` takes `AUTH_SECRET`
   from `DJANGO_SECRET_KEY` and `APP_URL` from `CSRF_TRUSTED_ORIGINS` while
   those are set; set `APP_URL` when `CSRF_TRUSTED_ORIGINS` names several
   addresses ([deployment.md](deployment.md#upgrade-from-the-django-version)).
3. Set `IAM_VERSION` to `0.3` and redeploy: Coolify reads the new
   `compose.coolify.yaml` from `stable`, `migrate` takes over the database,
   and everyone signs in again. The domain stays on `web`, port 8000.

## Backups

For [Meldgrove Backups](deployment.md#backups), set `BACKUP_URL` and
`BACKUP_TOKEN` (from the app's source in Backups) in the resource's
environment variables and redeploy. Each deploy pulls the backup agent's
image.

## Automatic updates

Set up [backups](updates.md#backups) first; the PostgreSQL resource has
scheduled backups.

1. Under Settings > Advanced, turn on API Access if it is off.
2. Under Keys & Tokens > API Tokens, create a token with only the `deploy` permission;
   clear `read`. Copy it at once; Coolify shows it only once.
3. Open the application's resource, select Webhooks, and copy Deploy Webhook. It looks
   like `https://coolify.example.invalid/api/v1/deploy?uuid=<resource>&force=false`.
4. In the resource's environment variables, set `UPDATE_DEPLOY_HOOK_URL` to the
   webhook and `UPDATE_DEPLOY_HOOK_TOKEN` to the token. Redeploy.

On Coolify the image tag set there decides what installs, instead of the channel in
the app:

| `IAM_VERSION` | Installs |
| --- | --- |
| `0.1` | every 0.1.x release, never 0.2.0 |
| `latest` | every release, also one whose notes list upgrade steps, before anyone took them |
| `preview` | every preview and release: the unstable channel |
| `0.1.0` | nothing |

When the tag points to a new image, the updater calls the deploy link (after the
cooldown, or at once from the update button), and Coolify pulls and deploys it. To
move to a new series, take its upgrade steps, then change `IAM_VERSION` and redeploy
([Upgrades](#upgrades)). With `latest`, watch the repository's releases on GitHub
(Watch > Custom > Releases) and take upgrade steps right away. `UPDATE_COOLDOWN`,
`UPDATE_WINDOW` and `TZ` work as on a server
([updates.md](updates.md#when-an-update-installs)).

To switch automatic updates off, clear `UPDATE_DEPLOY_HOOK_URL` and redeploy, or set
`IAM_VERSION` to an exact version.

### Protect the update service

With `UPDATER_TOKEN` set, the app sends it with each request to the updater, and the
updater refuses requests without it ([updates.md](updates.md#protect-the-update-service)).
Recommended for resources set up before it existed; without it, everything works as
before. Until it is done, Admin › Settings › Updates warns that the update service
takes requests without a token (with updater 0.2.4 or later, which reports it).

Do it once the tag in `IAM_VERSION` gives the release whose notes name this step, or
a newer one: each deploy pulls that tag, and an older version sends no token, so its
update button would get no answer. In the resource's environment variables, set
`UPDATER_TOKEN` to the output of `openssl rand -hex 32`, then redeploy: Coolify reads
`compose.coolify.yaml` from `stable` and pulls the images again.

## Moving an existing resource

Resources set up before `compose.coolify.yaml` existed read `/compose.yaml`. That file
is made for a server now: it has no deploy link and does not pull the images again, so
automatic updates stop with it. Once the release that brings `compose.coolify.yaml` is
out (its release notes name this step):

1. In the resource's configuration, set Docker Compose Location to
   `/compose.coolify.yaml` and save.
2. Keep the environment variables. Change `IAM_VERSION` when the release starts a new
   series.
3. Redeploy. The services and volumes keep their names, so the data stays.
