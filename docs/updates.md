# Updates

Each release publishes the production image `ghcr.io/meldgrove/iam` under its
version, its series and `latest` (see [releases](releases.md)). Each change on `main`
whose checks pass publishes a preview such as `0.2.1-preview.3` under its version and
`preview`.

The `updater` service in `compose.yaml` checks for new versions once an hour, shows them
to admins in the app, and installs them: through Coolify's deploy link, or through a
request in its volume `updater` that the Debian server carries out. Coolify or the
server pulls the image, runs the migrations, and replaces web and worker. It is
[Meldgrove Updater](https://github.com/meldgrove/updater), the same for every Meldgrove
app. Until it is [switched on](#switch-it-on), it only checks.

## Channels

| Channel | Offers | Installs automatically |
| --- | --- | --- |
| **Stable** (default) | releases | a release of the installed series, three days after it came out |
| **Preview** | every change on `main` as soon as its checks pass, and every release | within the hour |

Admins choose the channel in Admin › Settings › Updates. Preview brings new features
first, and now and then a new bug; previews have no release notes. Switching from
Preview back to Stable installs nothing and never goes back to an older version: the
app keeps the preview until a release is newer, since a database that a newer version
migrated may not work with an older one.

A release that operators must act on starts a new series (0.1 → 0.2; from 1.0.0,
1 → 2). Stable shows it as needing upgrade steps and never installs it on its own:
take the steps from its release notes, then install it from the update button
("Install anyway…"; [deployment.md](deployment.md#upgrades)).

On [Coolify](#on-coolify), the image tag in `IAM_VERSION` decides instead of the
channel, and the settings say so. A new series then needs a new tag: the update button
shows it without "Install anyway…".

## The update button

Admins (and only they) see an update icon in the top bar. A click checks for updates.
An available update shows a dot; the button then shows its changes, when it installs
automatically, and **Install now**. While it installs, the button shows the progress;
Meldgrove IAM restarts, and the page reloads once the installation has finished (on a
Debian server: once every service is healthy). When an installation fails, the server
goes back to the version before, and the button says why. Admin › Settings › Updates
shows the installed version, the channel, and when updates install automatically.

The app reaches the updater at `UPDATER_URL` ([configuration.md](configuration.md));
`compose.yaml` sets it. Read-only mode (`READ_ONLY=1`) does not hold back updates of
the app itself; it only leaves connected apps unchanged.

## When an update installs

A release installs three days after it came out, so that a faulty release can be
replaced before it reaches the deployment. `UPDATE_COOLDOWN` changes the time: `0`
installs within the hour, `7d` waits a week. Security fixes wait as well; install them
at once with the update button. Previews do not wait.

Coolify or the server replaces web and worker while it deploys. `UPDATE_WINDOW`, such
as `02:00-05:00` in the time zone `TZ` (default UTC), keeps automatic installs out of
working hours; the update button installs at any time. A version whose installation
failed does not install again automatically; a newer one does.

| Variable (`compose.yaml`) | Meaning |
| --- | --- |
| `UPDATE_DEPLOY_HOOK_URL`, `UPDATE_DEPLOY_HOOK_TOKEN` | Coolify's deploy link and its token ([On Coolify](#on-coolify)). |
| `UPDATE_COOLDOWN` | How long a stable release is out before it installs: `3d` (default), `12h`, `30m`, seconds. |
| `UPDATE_WINDOW`, `TZ` | Automatic installs only between two times of day. |
| `UPDATE_REGISTRY_USERNAME`, `UPDATE_REGISTRY_PASSWORD` | Only for an image in a private registry. |

## Switch it on

Set up [backups](#backups) first.

### On Coolify

1. Under Settings > Advanced, turn on API Access if it is off.
2. Under Keys & Tokens > API Tokens, create a token with only the `deploy` permission;
   clear `read`. Copy it at once; Coolify shows it only once.
3. Open the application's resource, select Webhooks, and copy Deploy Webhook. It looks
   like `https://coolify.example.invalid/api/v1/deploy?uuid=<resource>&force=false`.
4. In the resource's environment variables, set `UPDATE_DEPLOY_HOOK_URL` to the
   webhook and `UPDATE_DEPLOY_HOOK_TOKEN` to the token. Redeploy.

On Coolify the image tag set there decides what installs:

| `IAM_VERSION` | Installs |
| --- | --- |
| `0.1` | every 0.1.x release, never 0.2.0 |
| `latest` | every release, also one whose notes list upgrade steps, before anyone took them |
| `preview` | every preview and release: the unstable channel |
| `0.1.0` | nothing |

When the tag points to a new image, the updater calls the deploy link (after the
cooldown, or at once from the update button), and Coolify pulls and deploys it. To
move to a new series, take its upgrade steps, then change `IAM_VERSION` and redeploy
([deployment.md](deployment.md#upgrades)). With `latest`, watch the repository's
releases on GitHub (Watch > Custom > Releases) and take upgrade steps right away.

### On Debian

As root in `/srv/iam` ([deployment.md](deployment.md#debian)), with
`UPDATE_DEPLOY_HOOK_URL` empty:

```sh
docker compose up -d --pull always
docker compose run --rm -T --pull always updater host-setup > updater-setup.sh
sh updater-setup.sh
```

The first command starts the image that `IAM_VERSION` points to, which the updater
takes as installed. The second writes the setup script from the updater's image; read
it before you run it. The script installs a systemd unit that waits for the updater's
requests. For each installation it writes the exact version into `IAM_VERSION` in
`.env` (`0.1` becomes `0.1.3`, for example), pulls the image, and runs
`docker compose up --wait`, migrations first. When the app does not become healthy, it
restores `.env` and starts the version before. The channel is chosen in the app.
`journalctl -u meldgrove-updater-iam` shows each installation.

It installs images, not files: when a release changes `compose.yaml`, its upgrade
steps say to run `git pull` in `/srv/iam` first.

**Servers set up before the update button** (updater 0.1) keep following the tag in
`IAM_VERSION`, and the app says that the channel needs the host setup again. As root
in `/srv/iam`, run `git pull`, set `IAM_VERSION` in `.env` to the release that brings
the update button (or a newer one), then:

```sh
docker compose up -d --pull always
docker compose run --rm -T --pull always updater host-setup > updater-setup.sh
sh updater-setup.sh
```

`git pull` brings the `compose.yaml` of the newest release from the branch `stable`
(with updater 0.2). `docker compose up -d --pull always` starts that release of the
app, not only the new updater; the last command replaces the host setup.

## Backups

An automatic update runs without anyone present, so nobody takes a backup right
before it. Before switching updates on:

- Schedule encrypted, off-host backups of the PostgreSQL database, at least
  nightly. In Coolify, the PostgreSQL resource has scheduled backups.
- Keep `ENCRYPTION_KEY` with the backups' keys: without it, the connection
  secrets in a restored database cannot be read.
- Test a restore.

Releases can include migrations, so going back to an older release can need the
backup from before the update.

## Switch it off

- **Coolify:** clear `UPDATE_DEPLOY_HOOK_URL` and redeploy, or set `IAM_VERSION` to an
  exact version.
- **Debian:** as root in `/srv/iam`, run `sh updater-setup.sh remove`.

The updater keeps checking, and the update button keeps showing new versions, with
"Automatic updates are off on this server".
