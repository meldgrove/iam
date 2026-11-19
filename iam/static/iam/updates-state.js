// The update button's logic (docs/updates.md, "Button" and "Install flow"),
// without the page: which state an UpdatesResponse means, what the popover
// says, and when an installation is over. updates.js draws it; the tests in
// tests/js/ run this file in Node.
"use strict";

const IamUpdates = (() => {
  const FAILED = ["failed", "rolled-back"];
  const SLOW_AFTER_MS = 15 * 60 * 1000;
  const MAX_NOTES = 8;
  const STAGES = {
    requested: () => "Waiting for the server",
    pulling: () => "Downloading",
    starting: () => "Starting",
    "rolling-back": (before) => `Going back to ${before}`,
    deploying: () => "Deploying",
  };

  // One of: idle (no answer yet), unreachable, installing, checking, failed,
  // off, available, attention, current.
  function deriveState(response, { pending = null, checking = false } = {}) {
    if (pending) return "installing";
    if (!response) return checking ? "checking" : "idle";
    const updater = response.updater;
    if (!response.reachable || !updater) return "unreachable";
    if (updater.installing) return "installing";
    if (checking || updater.checking) return "checking";
    if (failedUpdate(updater)) return "failed";
    if (updater.mode === "off") return "off";
    if (updater.available) return "available";
    if (updater.attention) return "attention";
    return "current";
  }

  function failedUpdate(updater) {
    if (!updater.last || !FAILED.includes(updater.last.result)) return null;
    return [updater.available, updater.attention].find((update) => update && update.failed) || null;
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
        return "Automatic updates are off on this server";
      case "unreachable":
        return "No update service. See the deployment guide.";
      default:
        return "Check for updates";
    }
  }

  // Whether a click opens the popover; otherwise it checks (or, without an
  // update service, asks for the status again).
  function hasPopover(state, response) {
    if (["available", "attention", "failed", "installing"].includes(state)) return true;
    const updater = response && response.updater;
    return state === "off" && Boolean(updater && (updater.available || updater.attention));
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

  // What the popover shows, as plain data (docs/updates.md, "Popover").
  function popover(state, response, { pending = null, restarting = false, now = Date.now(), app = "The app", checking = false, error = "" } = {}) {
    const updater = (response && response.updater) || {};
    const installed = installedVersion(response);
    const model = {
      title: "",
      meta: "",
      preview: false,
      text: [],
      message: "",
      notes: [],
      more: "",
      url: "",
      urlLabel: "Release notes",
      installs: "",
      action: null,
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
      const slow = pending && now - pending.startedAt > SLOW_AFTER_MS;
      const before = (pending && (pending.installedBefore || pending.from)) || installed;
      model.title = `Installing ${installingVersion(response, pending)}…`;
      model.meta = restarting ? "Restarting…" : stage && STAGES[stage] ? STAGES[stage](before) : "";
      model.text = [`${app} restarts. This page reloads when it is back.`];
      if (slow) {
        model.message = "This takes longer than expected. Check the server (journalctl -u meldgrove-updater-iam).";
      }
      return model;
    }
    if (state === "failed") {
      const update = failedUpdate(updater);
      model.title = `Update to ${updater.last.version} failed`;
      model.message = updater.last.message || update.failed;
      model.text = [`${installed} is still running.`];
      model.url = update.url || "";
      model.action = {
        label: "Try again",
        primary: true,
        version: update.version,
        confirm: confirmation(update, update === updater.attention, app),
      };
      return model;
    }
    const attention = !updater.available && updater.attention;
    const update = updater.available || updater.attention;
    if (!update) return model;
    model.url = update.url || "";
    if (update.preview) model.urlLabel = "Changes";
    if (attention) {
      model.title = `${update.version} needs upgrade steps`;
      model.text = ["This release changes the setup. Take the upgrade steps from its release notes on the server first."];
    } else {
      model.title = "Update available";
      model.meta = [update.version, update.publishedAt ? ago(update.publishedAt, now) : ""].filter(Boolean).join(" · ");
      model.preview = Boolean(update.preview);
      const list = notes(update);
      model.notes = list.items;
      model.more = list.more;
    }
    if (updater.mode === "off") {
      model.installs = "Automatic updates are off on this server";
      return model;
    }
    if (!attention) model.installs = installsLine(update, now);
    model.action = {
      label: attention ? "Install anyway…" : "Install now",
      primary: !attention,
      version: update.version,
      confirm: confirmation(update, attention, app),
    };
    return model;
  }

  // The installation the button waits for, kept in sessionStorage so that a
  // reload during the restart keeps waiting.
  function pendingInstall(version, response, now) {
    const updater = (response && response.updater) || {};
    return {
      version,
      from: (response && response.app && response.app.version) || null,
      installedBefore: updater.installed || null,
      lastBefore: (updater.last && updater.last.finishedAt) || null,
      startedAt: now,
      outage: false,
    };
  }

  // "done", "failed", "slow" (over 15 minutes) or "waiting".
  function installOutcome(response, pending, now) {
    const app = (response && response.app) || {};
    if (app.version && app.version === pending.version) return "done";
    const updater = (response && response.updater) || {};
    const last = updater.last;
    const runs = updater.installing && updater.installing.version === pending.version;
    if (last && !runs && last.version === pending.version && last.finishedAt !== pending.lastBefore) {
      if (last.result === "installed") return "done";
      if (last.result === "accepted" && ((app.version || null) !== pending.from || pending.outage)) return "done";
      if (FAILED.includes(last.result)) return "failed";
    }
    return now - pending.startedAt > SLOW_AFTER_MS ? "slow" : "waiting";
  }

  return { deriveState, tooltip, hasPopover, popover, ago, installsLine, notes, pendingInstall, installOutcome };
})();

if (typeof module !== "undefined") module.exports = IamUpdates;
