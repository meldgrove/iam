// The update button's logic (docs/updates.md; the updater's API:
// https://github.com/meldgrove/updater#the-apps-update-button), without the
// page: which state an UpdatesResponse means, what the popover says, and when
// an installation is over. updates.js draws it; the tests in tests/js/ run
// this file in Node.
"use strict";

const IamUpdates = (() => {
  const FAILED = ["failed", "rolled-back"];
  const SLOW_AFTER_MS = 15 * 60 * 1000;
  // How long the button looks for an installation after an unclear answer to
  // its request (no answer, a timeout, a 5xx, an unreachable updater),
  // counted from the attempt's start on a timer of its own.
  const RECONCILE_MS = 60 * 1000;
  const VERSION = /^[0-9]+\.[0-9]+\.[0-9]+(-preview\.[0-9]+)?$/;
  const SIGNED_OUT = "The session ended or no longer has admin rights. Sign in again.";
  // The updater refuses installations while automatic updates are off.
  const OFF = "Automatic updates are off on this server";
  const MAX_NOTES = 8;
  const STAGES = {
    requested: () => "Waiting for the server",
    pulling: () => "Downloading",
    starting: () => "Starting",
    "rolling-back": (before) => `Going back to ${before}`,
    deploying: () => "Deploying",
  };

  // One of: idle (no answer yet), unreachable, installing, checking, failed,
  // off, available, attention, current. `failure` is the `last` of an
  // installation the button followed until it failed.
  function deriveState(response, { pending = null, checking = false, failure = null } = {}) {
    if (pending) return "installing";
    if (!response) return checking ? "checking" : "idle";
    const updater = response.updater;
    if (!response.reachable || !updater) return "unreachable";
    if (updater.installing) return "installing";
    if (checking || updater.checking) return "checking";
    // A newer offer comes first; the popover keeps the failure as a line.
    const failed = failedLast(updater, failure);
    if (failed && (offered(updater, failed.version) || !(updater.available || updater.attention))) return "failed";
    // Also with automatic updates off: the updater still offers new versions.
    if (updater.available) return "available";
    if (updater.attention) return "attention";
    if (updater.mode === "off") return "off";
    return "current";
  }

  // The `last` of a failed installation that the button shows: when the
  // updater marks its version as failed, or when the button followed it.
  function failedLast(updater, failure) {
    const last = updater.last;
    if (!last || !FAILED.includes(last.result)) return null;
    const marked = [updater.available, updater.attention].some(
      (update) => update && update.failed && update.version === last.version,
    );
    const followed = failure && failure.version === last.version && failure.finishedAt === last.finishedAt;
    return marked || followed ? last : null;
  }

  // The offer (available or attention) with this version, if still offered.
  function offered(updater, version) {
    return [updater.available, updater.attention].find((update) => update && update.version === version) || null;
  }

  function installedVersion(response) {
    return (response && response.updater && response.updater.installed) ||
      (response && response.app && response.app.version) ||
      "development";
  }

  function installingVersion(response, pending) {
    if (pending) return pending.version;
    const installing = response && response.updater && response.updater.installing;
    return installing ? installing.version : "";
  }

  function tooltip(state, response, pending) {
    const updater = (response && response.updater) || {};
    switch (state) {
      case "checking":
        return "Checking for updates…";
      case "current":
        return `Up to date · ${installedVersion(response)}`;
      case "available":
        return `Update available · ${updater.available.version}`;
      case "attention":
        return `${updater.attention.version} needs upgrade steps`;
      case "installing":
        return `Installing ${installingVersion(response, pending)}…`;
      case "failed":
        return `Update to ${updater.last.version} failed`;
      case "off":
        return OFF;
      case "unreachable":
        return "No update service. See the deployment guide.";
      default:
        return "Check for updates";
    }
  }

  // Whether a click opens the popover; otherwise it checks (also without an
  // update service: the check's error says why).
  function hasPopover(state) {
    return ["available", "attention", "failed", "installing"].includes(state);
  }

  function ago(iso, now) {
    const time = Date.parse(iso);
    if (Number.isNaN(time)) return "";
    const minutes = Math.floor(Math.max(0, now - time) / 60000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
    const days = Math.floor(hours / 24);
    return days === 1 ? "1 day ago" : `${days} days ago`;
  }

  // The app's date style (d.m.Y H:i), in the browser's time zone.
  function formatDate(time) {
    const date = new Date(time);
    const pad = (value) => String(value).padStart(2, "0");
    return `${pad(date.getDate())}.${pad(date.getMonth() + 1)}.${date.getFullYear()} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function installsLine(update, now) {
    if (!update.installsAt) return "";
    const time = Date.parse(update.installsAt);
    if (Number.isNaN(time)) return "";
    if (time <= now) return "Installs automatically at the next check";
    return `Installs automatically on ${formatDate(time)}`;
  }

  function notes(update) {
    const shown = (update.notes || []).slice(0, MAX_NOTES);
    const more = Math.max(0, (update.changes || 0) - shown.length);
    return { items: shown, more: more ? `and ${more} more changes` : "" };
  }

  // Stable on a platform or an updater 0.1 host setup: the image tag decides,
  // and the updater refuses a version of a newer series.
  function tagText(update, app) {
    return `The image tag set where ${app} is deployed does not reach ${update.version}. ` +
      "Take the upgrade steps from its release notes, then change the tag there.";
  }

  function confirmation(update, attention, app) {
    const restart = `${app} restarts and is unavailable for a moment. Database changes run automatically.`;
    if (attention) {
      return {
        title: `Install ${update.version} without its upgrade steps?`,
        text: `Its release notes list upgrade steps to take on the server first. ${restart}`,
        url: update.url || "",
        danger: true,
      };
    }
    return { title: `Install ${update.version} now?`, text: restart, url: "", danger: false };
  }

  // "0.2.1 failed: <message>": an earlier failure under a newer offer.
  function failureLine(last) {
    return last.message ? `${last.version} failed: ${last.message}` : `${last.version} failed`;
  }

  // What the popover shows, as plain data. `waiting`: the button follows the
  // installation and reloads the page once it is over.
  function popover(state, response, { pending = null, waiting = Boolean(pending), restarting = false, now = Date.now(), app = "The app", checking = false, error = "", failure = null } = {}) {
    const updater = (response && response.updater) || {};
    const installed = installedVersion(response);
    const model = {
      title: "",
      meta: "",
      preview: false,
      text: [],
      message: "",
      failed: "",
      notes: [],
      more: "",
      url: "",
      urlLabel: "Release notes",
      installs: "",
      action: null,
      stop: "",
      error: error || "",
      footer: {
        installed: `Installed: ${installed} · ${updater.channel === "preview" ? "Preview" : "Stable"}`,
        checked: checking || updater.checking
          ? "Checking for updates…"
          : updater.checkedAt ? `Checked ${ago(updater.checkedAt, now)}` : "Not checked yet",
        checkError: updater.error ? `Last check failed: ${updater.error}` : "",
      },
    };
    if (state === "installing") {
      const stage = updater.installing && updater.installing.stage;
      const slow = waiting && now - pending.startedAt >= SLOW_AFTER_MS;
      const before = (pending && (pending.installedBefore || pending.from)) || installed;
      model.title = `Installing ${installingVersion(response, pending)}…`;
      model.meta = restarting ? "Restarting…" : stage && STAGES[stage] ? STAGES[stage](before) : "";
      // A reload is promised only while the button waits for the installation.
      model.text = [waiting ? `${app} restarts. This page reloads when it is back.` : `${app} restarts.`];
      if (slow) {
        model.message = "This takes longer than expected. Check the server (journalctl -u meldgrove-updater-iam).";
        model.stop = "Stop waiting";
      }
      return model;
    }
    if (state === "failed") {
      const last = failedLast(updater, failure) || updater.last;
      const update = offered(updater, last.version);
      const attention = Boolean(update) && update !== updater.available;
      model.title = `Update to ${last.version} failed`;
      model.message = last.message || (update && update.failed) || "";
      model.text = [`${installed} is still running.`];
      model.url = (update && update.url) || "";
      if (updater.mode === "off") {
        model.installs = OFF;
        return model;
      }
      // Try again only while the version is still offered and can install.
      if (!update || (attention && updater.channelLocked)) return model;
      model.action = {
        label: "Try again",
        primary: true,
        version: update.version,
        confirm: confirmation(update, attention, app),
      };
      return model;
    }
    const attention = !updater.available && updater.attention;
    const update = updater.available || updater.attention;
    if (!update) return model;
    const failed = failedLast(updater, failure);
    if (failed && failed.version !== update.version) model.failed = failureLine(failed);
    model.url = update.url || "";
    if (update.preview) model.urlLabel = "Changes";
    if (attention) {
      model.title = `${update.version} needs upgrade steps`;
      model.text = [updater.channelLocked
        ? tagText(update, app)
        : "This release changes the setup. Take the upgrade steps from its release notes on the server first."];
    } else {
      model.title = "Update available";
      model.meta = [update.version, update.publishedAt ? ago(update.publishedAt, now) : ""].filter(Boolean).join(" · ");
      model.preview = Boolean(update.preview);
      const list = notes(update);
      model.notes = list.items;
      model.more = list.more;
    }
    if (updater.mode === "off") {
      model.installs = OFF;
      return model;
    }
    if (!attention) model.installs = installsLine(update, now);
    // The updater refuses a newer series while the image tag decides.
    if (attention && updater.channelLocked) return model;
    model.action = {
      label: attention ? "Install anyway…" : "Install now",
      primary: !attention,
      version: update.version,
      confirm: confirmation(update, attention, app),
    };
    return model;
  }

  // The installation the button waits for (an "attempt"), kept in
  // sessionStorage so that a reload during the restart keeps waiting. It is
  // remembered before the request; `confirmed` once the updater reports it.
  function pendingInstall(version, response, now, { confirmed = false } = {}) {
    const updater = (response && response.updater) || {};
    const last = updater.last;
    return {
      version,
      from: (response && response.app && response.app.version) || null,
      installedBefore: updater.installed || null,
      startedAt: now,
      // installing.startedAt once a status reports the installation (the
      // updater's clock): with the version, the installation's identity.
      serverStartedAt: null,
      // The `last` of the newest status before the request (the "earlier
      // result"): never this attempt's own result.
      earlier: last ? { version: last.version, result: last.result, finishedAt: last.finishedAt } : null,
      confirmed,
      // Why the answer to the request was unclear.
      reason: "",
      // The latest error while looking for it, and whether the latest answer
      // was a reachable status without it.
      error: "",
      answered: false,
      outage: false,
    };
  }

  // A `last` is fresh when it is not the earlier result and finished at or
  // after the installation started: installing.startedAt when a status
  // reported it, otherwise the attempt's start cut to whole seconds (the
  // updater writes whole seconds).
  function fresh(last, pending) {
    const earlier = pending.earlier;
    if (earlier && earlier.version === last.version && earlier.result === last.result &&
      earlier.finishedAt === last.finishedAt) return false;
    const finished = Date.parse(last.finishedAt);
    if (Number.isNaN(finished)) return false;
    const since = pending.serverStartedAt != null ? pending.serverStartedAt : Math.floor(pending.startedAt / 1000) * 1000;
    return finished >= since;
  }

  function freshLast(updater, pending) {
    const last = updater.last;
    return last && last.version === pending.version && fresh(last, pending) ? last : null;
  }

  // The attempt after a status answer (from a poll, a check or a refusal):
  // an installation that the updater reports is followed, also one that
  // another admin or an automatic install started; an attempt counts as
  // confirmed once the updater reports it.
  function follow(pending, response, now) {
    const updater = response && response.reachable && response.updater;
    if (!updater) return pending;
    const installing = updater.installing;
    if (installing) {
      const started = Date.parse(installing.startedAt);
      const serverStartedAt = Number.isNaN(started) ? null : started;
      if (pending && pending.version === installing.version) {
        if (pending.confirmed && pending.serverStartedAt === serverStartedAt) return pending;
        return { ...pending, confirmed: true, serverStartedAt };
      }
      return { ...pendingInstall(installing.version, response, now, { confirmed: true }), serverStartedAt };
    }
    if (pending && !pending.confirmed && freshLast(updater, pending)) return { ...pending, confirmed: true };
    return pending;
  }

  // The UpdatesResponse in an answer of the app's routes, if any.
  function statusOf(answer) {
    const body = answer.body;
    return body && body.app && body.updater !== undefined ? body : null;
  }

  // Why an answer says nothing about the installation.
  function problem(answer) {
    const body = statusOf(answer);
    if (body && body.error) return body.error;
    return answer.status ? `The server answered HTTP ${answer.status}.` : "No answer from the server.";
  }

  // What the answer to POST install means: the attempt to follow (null:
  // forget it) and the error to show. {status, body, signedOut}: status 0
  // without an answer.
  function answerInstall(pending, answer, now) {
    const body = statusOf(answer);
    if (answer.signedOut) return { pending: null, error: SIGNED_OUT };
    if (answer.status === 202) {
      // Accepted, also when the answer cannot be read.
      const confirmed = { ...pending, confirmed: true };
      return { pending: body && body.reachable ? follow(confirmed, body, now) : confirmed, error: "" };
    }
    if (answer.status >= 400 && answer.status < 500) {
      // Refused, unless another installation is faster: follow that one.
      const installing = body && body.reachable && body.updater && body.updater.installing;
      if (installing) return { pending: follow(null, body, now), error: "" };
      return { pending: null, error: problem(answer) };
    }
    // Unclear: the updater may have accepted it. Keep the attempt and look.
    const reason = problem(answer);
    return { pending: { ...pending, reason, error: reason }, error: "" };
  }

  // What a status answer while waiting says, besides the installations that
  // follow() takes: no answer, a 5xx or an unreachable updater is an outage
  // (the app or the updater restarts), with its error; a reachable status
  // that does not report an unconfirmed attempt is noted as `answered`.
  function observe(pending, answer) {
    const body = statusOf(answer);
    if (!body || answer.status >= 500 || !body.reachable || !body.updater) {
      return { ...pending, outage: true, answered: false, error: problem(answer) };
    }
    return pending.confirmed ? pending : { ...pending, answered: true };
  }

  // When the wait for an unconfirmed attempt ends, whatever the polls return.
  function deadline(pending) {
    return pending.startedAt + RECONCILE_MS;
  }

  // The error for an attempt that no status confirmed by its deadline.
  function unconfirmed(pending) {
    if (pending.answered) {
      return pending.reason ? `The installation did not start. ${pending.reason}` : "The installation did not start.";
    }
    const error = pending.error || pending.reason;
    return error ? `The installation could not be confirmed. ${error}` : "The installation could not be confirmed.";
  }

  // The installation that Stop waiting ignores from then on: by the identity
  // stored on the attempt (version and the updater's start time), otherwise
  // by the version and the attempt's start.
  function dismissal(pending) {
    return {
      version: pending.version,
      startedAt: pending.serverStartedAt,
      since: Math.floor(pending.startedAt / 1000) * 1000,
    };
  }

  function dismisses(dismissed, installing) {
    if (!dismissed || !installing || installing.version !== dismissed.version) return false;
    const started = Date.parse(installing.startedAt);
    if (dismissed.startedAt != null) return started === dismissed.startedAt;
    return Number.isNaN(started) || started >= dismissed.since;
  }

  // "done", "failed" or "waiting", for a reachable status answer received
  // after the attempt started. An unconfirmed attempt ends at its deadline.
  function installOutcome(response, pending) {
    const updater = response && response.reachable && response.updater;
    if (updater) {
      const appVersion = (response.app && response.app.version) || null;
      const last = freshLast(updater, pending);
      const runs = updater.installing && updater.installing.version === pending.version;
      if (last && FAILED.includes(last.result)) return "failed";
      // On a server the result comes only once every service is healthy, and
      // it may still roll back, even when the app already answers with it.
      if (!runs) {
        if (last && last.result === "installed") return "done";
        if (appVersion === pending.version) return "done";
        // Platform: the deploy link took it; the app's version tells when it runs.
        if (last && last.result === "accepted" && pending.outage && (!appVersion || !VERSION.test(pending.version))) {
          return "done";
        }
      }
    }
    return "waiting";
  }

  return {
    SIGNED_OUT,
    SLOW_AFTER_MS,
    deriveState,
    tooltip,
    hasPopover,
    popover,
    ago,
    installsLine,
    notes,
    pendingInstall,
    follow,
    answerInstall,
    observe,
    deadline,
    unconfirmed,
    dismissal,
    dismisses,
    installOutcome,
  };
})();

if (typeof module !== "undefined") module.exports = IamUpdates;
