// Unit tests of the update button's logic (iam/static/iam/updates-state.js):
//   node --test tests/js/updates.test.cjs
const test = require("node:test");
const assert = require("node:assert/strict");
const logic = require("../../iam/static/iam/updates-state.js");

const NOW = Date.parse("2026-11-19T12:00:00Z");
const hoursAgo = (hours) => new Date(NOW - hours * 3_600_000).toISOString();

function release(version, extra = {}) {
  return {
    version,
    preview: false,
    publishedAt: hoursAgo(50),
    url: `https://github.com/meldgrove/iam/releases/tag/v${version}`,
    notes: ["Show who changed a page last", "Update dependencies"],
    changes: 2,
    installsAt: new Date(NOW + 42 * 3_600_000).toISOString(),
    failed: null,
    ...extra,
  };
}

function response(updater = {}, app = { version: "0.2.0" }) {
  return {
    app,
    reachable: true,
    error: null,
    updater: {
      updater: "0.2.0",
      mode: "host",
      channel: "stable",
      channelLocked: null,
      installed: "0.2.0",
      available: null,
      attention: null,
      installing: null,
      last: null,
      checking: false,
      checkedAt: hoursAgo(0.1),
      error: null,
      automatic: true,
      cooldown: "3d",
      window: null,
      timezone: "UTC",
      ...updater,
    },
  };
}

const failedRelease = release("0.2.1", { failed: "exit 1" });
const failedLast = { version: "0.2.1", result: "rolled-back", message: "migrate failed. 0.2.0 runs again.", finishedAt: hoursAgo(1) };

test("each status has its button state", () => {
  const cases = [
    [null, "idle"],
    [{ app: { version: null }, reachable: false, updater: null, error: "x" }, "unreachable"],
    [response(), "current"],
    [response({ checking: true }), "checking"],
    [response({ available: release("0.2.1") }), "available"],
    [response({ attention: release("0.3.0") }), "attention"],
    [response({ available: release("0.2.1"), attention: release("0.3.0") }), "available"],
    [response({ available: release("0.2.1"), installing: { version: "0.2.1", stage: "pulling" } }), "installing"],
    [response({ available: failedRelease, last: failedLast }), "failed"],
    [response({ available: release("0.2.1"), last: failedLast }), "available"],
    [response({ mode: "off", automatic: false }), "off"],
    // Off installs nothing, but still offers new versions.
    [response({ mode: "off", automatic: false, available: release("0.2.1") }), "available"],
    [response({ mode: "off", automatic: false, attention: release("0.3.0") }), "attention"],
  ];
  for (const [answer, state] of cases) {
    assert.equal(logic.deriveState(answer), state, JSON.stringify(answer && answer.updater));
  }
  assert.equal(logic.deriveState(response(), { checking: true }), "checking");
  assert.equal(logic.deriveState(response(), { pending: { version: "0.2.1" } }), "installing");
  assert.equal(logic.deriveState(null, { pending: { version: "0.2.1" } }), "installing");
});

test("tooltips", () => {
  assert.equal(logic.tooltip("current", response()), "Up to date · 0.2.0");
  assert.equal(logic.tooltip("checking", response()), "Checking for updates…");
  assert.equal(logic.tooltip("off", response()), "Automatic updates are off on this server");
  assert.equal(logic.tooltip("unreachable", null), "No update service. See the deployment guide.");
  assert.equal(logic.tooltip("installing", response(), { version: "0.2.1" }), "Installing 0.2.1…");
  assert.equal(logic.tooltip("current", response({ installed: null }, { version: null })), "Up to date · development");
});

test("a click opens the popover only when there is something to show", () => {
  for (const state of ["available", "attention", "failed", "installing"]) assert.equal(logic.hasPopover(state), true);
  for (const state of ["idle", "current", "checking", "off", "unreachable"]) assert.equal(logic.hasPopover(state), false);
});

test("the available popover", () => {
  const notes = Array.from({ length: 15 }, (_, i) => `Change ${i + 1}`);
  const answer = response({ available: release("0.2.1", { notes, changes: 40 }) });
  const model = logic.popover("available", answer, { now: NOW, app: "Meldgrove IAM" });
  assert.equal(model.title, "Update available");
  assert.equal(model.meta, "0.2.1 · 2 days ago");
  assert.equal(model.notes.length, 8);
  assert.equal(model.more, "and 32 more changes");
  assert.match(model.installs, /^Installs automatically on \d\d\.\d\d\.2026 \d\d:\d\d$/);
  assert.equal(model.action.label, "Install now");
  assert.equal(model.action.primary, true);
  assert.equal(model.urlLabel, "Release notes");
  assert.equal(model.action.confirm.title, "Install 0.2.1 now?");
  assert.equal(model.action.confirm.text, "Meldgrove IAM restarts and is unavailable for a moment. Database changes run automatically.");
  assert.equal(model.footer.installed, "Installed: 0.2.0 · Stable");
  assert.equal(model.footer.checked, "Checked 6 min ago");

  const due = response({ available: release("0.2.1", { installsAt: hoursAgo(1) }) });
  assert.equal(logic.popover("available", due, { now: NOW }).installs, "Installs automatically at the next check");
  const manual = response({ available: release("0.2.1", { installsAt: null }) });
  assert.equal(logic.popover("available", manual, { now: NOW }).installs, "");
  const preview = response({ channel: "preview", available: release("0.2.1-preview.3", { preview: true }) });
  const previewModel = logic.popover("available", preview, { now: NOW });
  assert.equal(previewModel.preview, true);
  assert.equal(previewModel.urlLabel, "Changes");
  assert.equal(previewModel.footer.installed, "Installed: 0.2.0 · Preview");
});

test("the attention, failed, off and installing popovers", () => {
  const attention = logic.popover("attention", response({ attention: release("0.3.0") }), { now: NOW, app: "Meldgrove IAM" });
  assert.equal(attention.title, "0.3.0 needs upgrade steps");
  assert.equal(attention.action.label, "Install anyway…");
  assert.equal(attention.action.primary, false);
  assert.equal(attention.action.confirm.title, "Install 0.3.0 without its upgrade steps?");
  assert.equal(attention.action.confirm.url, "https://github.com/meldgrove/iam/releases/tag/v0.3.0");

  const failed = logic.popover("failed", response({ available: failedRelease, last: failedLast }), { now: NOW });
  assert.equal(failed.title, "Update to 0.2.1 failed");
  assert.equal(failed.message, "migrate failed. 0.2.0 runs again.");
  assert.deepEqual(failed.text, ["0.2.0 is still running."]);
  assert.equal(failed.action.label, "Try again");

  const off = logic.popover("available", response({ mode: "off", available: release("0.2.1") }), { now: NOW });
  assert.equal(off.title, "Update available");
  assert.equal(off.action, null);
  assert.equal(off.installs, "Automatic updates are off on this server");
  const offFailed = logic.popover("failed", response({ mode: "off", available: failedRelease, last: failedLast }), { now: NOW });
  assert.equal(offFailed.action, null);

  const installing = response({ installing: { version: "0.2.1", stage: "rolling-back", startedAt: hoursAgo(0.1), manual: true } });
  const pending = logic.pendingInstall("0.2.1", response(), NOW - 60_000);
  const model = logic.popover("installing", installing, { pending, now: NOW, app: "Meldgrove IAM" });
  assert.equal(model.title, "Installing 0.2.1…");
  assert.equal(model.meta, "Going back to 0.2.0");
  assert.deepEqual(model.text, ["Meldgrove IAM restarts. This page reloads when it is back."]);
  assert.equal(model.message, "");
  assert.equal(logic.popover("installing", installing, { pending, now: NOW, restarting: true }).meta, "Restarting…");
  assert.equal(model.stop, "");
  const slow = logic.popover("installing", installing, { pending, now: NOW + 16 * 60_000 });
  assert.match(slow.message, /^This takes longer than expected\. Check the server \(journalctl -u meldgrove-updater-iam\)\.$/);
  assert.equal(slow.stop, "Stop waiting");
});

test("an installation ends when the new version answers", () => {
  const before = response({ available: release("0.2.1") });
  const pending = logic.pendingInstall("0.2.1", before, NOW, { confirmed: true });
  assert.equal(logic.installOutcome(before, pending, NOW), "waiting");
  assert.equal(logic.installOutcome(response({}, { version: "0.2.1" }), pending, NOW), "done");
  const installed = { version: "0.2.1", result: "installed", message: null, finishedAt: hoursAgo(0) };
  assert.equal(logic.installOutcome(response({ last: installed }), pending, NOW), "done");
  // Platform: done once the app answers with the version, or, without an app
  // version, once it answers again after an outage.
  const accepted = { ...installed, result: "accepted" };
  assert.equal(logic.installOutcome(response({ last: accepted }), pending, NOW), "waiting");
  assert.equal(logic.installOutcome(response({ last: accepted }), { ...pending, outage: true }, NOW), "waiting");
  assert.equal(logic.installOutcome(response({ last: accepted }, { version: "0.2.1" }), pending, NOW), "done");
  assert.equal(logic.installOutcome(response({ last: accepted }, { version: null }), pending, NOW), "waiting");
  assert.equal(logic.installOutcome(response({ last: accepted }, { version: null }), { ...pending, outage: true }, NOW), "done");
  const rolledBack = { ...installed, result: "rolled-back" };
  assert.equal(logic.installOutcome(response({ last: rolledBack }), pending, NOW), "failed");
  assert.equal(logic.installOutcome(before, pending, NOW + 16 * 60_000), "slow");
});

test("the app's new version is not enough while the updater still installs it", () => {
  const pending = logic.pendingInstall("0.2.1", response({ available: release("0.2.1") }), NOW, { confirmed: true });
  const starting = { version: "0.2.1", stage: "starting", startedAt: hoursAgo(0), manual: true };
  const answer = response({ available: release("0.2.1"), installing: starting }, { version: "0.2.1" });
  assert.equal(logic.installOutcome(answer, pending, NOW), "waiting");
  assert.equal(logic.deriveState(answer), "installing");
  // The updater restarts too: no status, no result yet.
  const unreachable = { app: { version: "0.2.1" }, reachable: false, updater: null, error: "No connection." };
  assert.equal(logic.installOutcome(unreachable, pending, NOW), "waiting");
  // It rolls back after all.
  const rolledBack = { version: "0.2.1", result: "rolled-back", message: "worker is unhealthy. 0.2.0 runs again.", finishedAt: hoursAgo(0) };
  assert.equal(logic.installOutcome(response({ last: rolledBack }, { version: "0.2.0" }), pending, NOW), "failed");
});

test("a failure is shown from last.message, also when no offer is marked failed", () => {
  const pending = logic.pendingInstall("0.2.1", response({ available: release("0.2.1") }), NOW, { confirmed: true });
  const last = { version: "0.2.1", result: "failed", message: "The server did not take the request within 2 minutes.", finishedAt: hoursAgo(0) };
  const answer = response({ available: release("0.2.1"), last });
  assert.equal(logic.installOutcome(answer, pending, NOW), "failed");
  assert.equal(logic.deriveState(answer), "available", "without the followed failure");
  assert.equal(logic.deriveState(answer, { failure: last }), "failed");
  const model = logic.popover("failed", answer, { now: NOW, failure: last });
  assert.equal(model.title, "Update to 0.2.1 failed");
  assert.equal(model.message, "The server did not take the request within 2 minutes.");
  assert.equal(model.action.label, "Try again");
  assert.equal(model.action.version, "0.2.1");
  // Try again only while the version is still offered.
  const gone = response({ last });
  assert.equal(logic.deriveState(gone, { failure: last }), "failed");
  assert.equal(logic.popover("failed", gone, { now: NOW, failure: last }).action, null);
  // The attention offer keeps its upgrade-steps confirmation.
  const attention = response({ attention: release("0.3.0"), last: { ...last, version: "0.3.0" } });
  const retry = logic.popover("failed", attention, { now: NOW, failure: attention.updater.last }).action;
  assert.equal(retry.confirm.title, "Install 0.3.0 without its upgrade steps?");
});

test("a result from before the attempt is ignored", () => {
  const failed = { version: "0.2.1", result: "rolled-back", message: "migrate failed", finishedAt: hoursAgo(0.5) };
  const installed = { ...failed, result: "installed", message: null };
  const pending = logic.pendingInstall("0.2.1", response(), NOW, { confirmed: true });
  assert.equal(logic.installOutcome(response({ available: failedRelease, last: failed }), pending, NOW), "waiting");
  assert.equal(logic.installOutcome(response({ last: installed }), pending, NOW), "waiting");
  // The updater's own start time counts too (the browser's clock may run ahead).
  const startedAt = hoursAgo(1);
  const followed = logic.follow(pending, response({ installing: { version: "0.2.1", stage: "pulling", startedAt, manual: true } }), NOW);
  assert.equal(followed.serverStartedAt, Date.parse(startedAt));
  assert.equal(logic.installOutcome(response({ last: installed }), followed, NOW), "done");
});

test("an upgrade the image tag does not reach offers no install", () => {
  const locked = "The image tag 0.2 set where the app is deployed decides which updates install. Change it there.";
  const answer = response({ mode: "platform", channelLocked: locked, attention: release("0.3.0") });
  assert.equal(logic.deriveState(answer), "attention");
  const model = logic.popover("attention", answer, { now: NOW, app: "Meldgrove IAM" });
  assert.equal(model.title, "0.3.0 needs upgrade steps");
  assert.equal(model.action, null);
  assert.deepEqual(model.text, [
    "The image tag set where Meldgrove IAM is deployed does not reach 0.3.0. Take the upgrade steps from its release notes, then change the tag there.",
  ]);
  // A release in the tag's series still installs through the deploy link.
  const both = logic.popover("available", response({ mode: "platform", channelLocked: locked, available: release("0.2.1"), attention: release("0.3.0") }), { now: NOW });
  assert.equal(both.action.label, "Install now");
  const failed = { version: "0.3.0", result: "failed", message: "not offered", finishedAt: hoursAgo(0) };
  const retry = response({ mode: "platform", channelLocked: locked, attention: release("0.3.0"), last: failed });
  assert.equal(logic.popover("failed", retry, { now: NOW, failure: failed }).action, null);
});

test("the answer to the install request decides whether the attempt is kept", () => {
  const before = response({ available: release("0.2.1") });
  const pending = logic.pendingInstall("0.2.1", before, NOW);
  assert.equal(pending.confirmed, false);
  const running = response({ available: release("0.2.1"), installing: { version: "0.2.1", stage: "requested", startedAt: hoursAgo(0), manual: true } });

  const accepted = logic.answerInstall(pending, { status: 202, body: running }, NOW);
  assert.equal(accepted.pending.confirmed, true);
  assert.equal(accepted.error, "");

  // Unclear: the updater may have accepted it before the answer got lost.
  const unclear = [
    [{ status: 0, body: null }, "No answer from the server."],
    [{ status: 502, body: null }, "The server answered HTTP 502."],
    [{ status: 200, body: { app: { version: "0.2.0" }, reachable: false, updater: null, error: "The update service did not answer within 10 s." } }, "The update service did not answer within 10 s."],
  ];
  for (const [answer, reason] of unclear) {
    const result = logic.answerInstall(pending, answer, NOW);
    assert.equal(result.pending.version, "0.2.1", reason);
    assert.equal(result.pending.confirmed, false, reason);
    assert.equal(result.pending.reason, reason);
    assert.equal(result.error, "");
  }

  const refused = logic.answerInstall(pending, { status: 409, body: { ...before, error: "0.2.1 is not offered; check again." } }, NOW);
  assert.equal(refused.pending, null);
  assert.equal(refused.error, "0.2.1 is not offered; check again.");

  // Another admin or an automatic install was faster: follow that one.
  const automatic = response({ available: release("0.2.2"), installing: { version: "0.2.2", stage: "pulling", startedAt: hoursAgo(0), manual: false } });
  const faster = logic.answerInstall(pending, { status: 409, body: { ...automatic, error: "0.2.2 is being installed." } }, NOW);
  assert.equal(faster.pending.version, "0.2.2");
  assert.equal(faster.pending.confirmed, true);
  assert.equal(faster.error, "");

  const signedOut = logic.answerInstall(pending, { status: 0, body: null, signedOut: true }, NOW);
  assert.equal(signedOut.pending, null);
  assert.equal(signedOut.error, logic.SIGNED_OUT);
});

test("an unclear request is settled by the status within a minute", () => {
  const before = response({ available: release("0.2.1") });
  const pending = { ...logic.pendingInstall("0.2.1", before, NOW), reason: "No answer from the server." };
  assert.equal(logic.installOutcome(before, pending, NOW + 30_000), "waiting");
  assert.equal(logic.installOutcome(before, pending, NOW + 61_000), "not-started");
  // Also without the updater; but while the app does not answer, or once it
  // restarted, nothing is settled.
  const unreachable = { app: { version: "0.2.0" }, reachable: false, updater: null, error: "No connection." };
  assert.equal(logic.installOutcome(unreachable, pending, NOW + 61_000), "not-started");
  assert.equal(logic.installOutcome(null, pending, NOW + 61_000), "waiting");
  assert.equal(logic.installOutcome(unreachable, { ...pending, outage: true }, NOW + 61_000), "waiting");
  // The status reports it: follow it.
  const running = response({ available: release("0.2.1"), installing: { version: "0.2.1", stage: "pulling", startedAt: hoursAgo(0), manual: true } });
  const followed = logic.follow(pending, running, NOW + 2000);
  assert.equal(followed.confirmed, true);
  assert.equal(logic.installOutcome(running, followed, NOW + 61_000), "waiting");
  // Or a fresh result for it.
  const last = { version: "0.2.1", result: "installed", message: null, finishedAt: new Date(NOW + 5000).toISOString() };
  assert.equal(logic.follow(pending, response({ last }), NOW + 6000).confirmed, true);
  assert.equal(logic.installOutcome(response({ last }), pending, NOW + 61_000), "done");
});

test("a status, a check or a refusal that reports an installation is followed", () => {
  const automatic = response({ available: release("0.2.1"), installing: { version: "0.2.1", stage: "pulling", startedAt: hoursAgo(0), manual: false } });
  const followed = logic.follow(null, automatic, NOW);
  assert.equal(followed.version, "0.2.1");
  assert.equal(followed.confirmed, true);
  assert.equal(followed.installedBefore, "0.2.0");
  assert.equal(logic.follow(null, response({ available: release("0.2.1") }), NOW), null);
  assert.equal(logic.follow(followed, automatic, NOW), followed, "unchanged while it runs");
  const unreachable = { app: { version: "0.2.0" }, reachable: false, updater: null, error: "x" };
  assert.equal(logic.follow(null, unreachable, NOW), null);
});

test("trying again ignores the earlier failure until a new result", () => {
  const before = response({ available: failedRelease, last: failedLast });
  const pending = logic.pendingInstall("0.2.1", before, NOW);
  assert.equal(logic.installOutcome(before, pending, NOW), "waiting");
  const running = response({ available: failedRelease, last: failedLast, installing: { version: "0.2.1", stage: "pulling" } });
  assert.equal(logic.installOutcome(running, pending, NOW), "waiting");
  const again = { ...failedLast, finishedAt: hoursAgo(0) };
  assert.equal(logic.installOutcome(response({ available: failedRelease, last: again }), pending, NOW), "failed");
});

test("relative times", () => {
  assert.equal(logic.ago(hoursAgo(0), NOW), "just now");
  assert.equal(logic.ago(new Date(NOW + 60_000).toISOString(), NOW), "just now");
  assert.equal(logic.ago(hoursAgo(5 / 60), NOW), "5 min ago");
  assert.equal(logic.ago(hoursAgo(1), NOW), "1 hour ago");
  assert.equal(logic.ago(hoursAgo(26), NOW), "1 day ago");
  assert.equal(logic.ago(hoursAgo(50), NOW), "2 days ago");
  assert.equal(logic.ago("not a date", NOW), "");
});

// Answers captured from Meldgrove Updater 0.2 (tests/fixtures/updater-0.2).
const fs = require("node:fs");
const path = require("node:path");

const FIXTURES = path.join(__dirname, "..", "fixtures", "updater-0.2");
const sample = (name) => JSON.parse(fs.readFileSync(path.join(FIXTURES, name), "utf8"));
const sampleResponse = (name) => {
  const updater = sample(name);
  return { app: { version: updater.installed }, reachable: true, updater, error: null };
};

test("each answer of Updater 0.2 has its button state", () => {
  const expected = {
    "status-off-available-attention.json": "available",
    "status-host-available.json": "available",
    "status-platform-locked.json": "current",
    "status-installing.json": "installing",
    "status-failed.json": "failed",
    "status-preview.json": "failed",
    "install-202.json": "installing",
  };
  const names = fs.readdirSync(FIXTURES).filter((name) => name.endsWith(".json"));
  assert.deepEqual(names.sort(), Object.keys(expected).sort());
  for (const [name, state] of Object.entries(expected)) {
    const answer = sampleResponse(name);
    assert.equal(logic.deriveState(answer), state, name);
    assert.equal(logic.popover(state, answer, { now: NOW }).title !== "", logic.hasPopover(state), name);
  }
});

test("the popovers of Updater 0.2's answers", () => {
  const off = logic.popover("available", sampleResponse("status-off-available-attention.json"), { now: NOW });
  assert.equal(off.meta.startsWith("0.2.1"), true);
  assert.equal(off.action, null);
  assert.equal(off.installs, "Automatic updates are off on this server");
  const preview = logic.popover("failed", sampleResponse("status-preview.json"), { now: NOW });
  assert.equal(preview.title, "Update to 0.1.3 failed");
  assert.equal(preview.message, "container library-web-1 is unhealthy");
  assert.equal(preview.action.version, "0.1.3");
  const installing = logic.popover("installing", sampleResponse("status-installing.json"), { now: NOW });
  assert.equal(installing.meta, "Downloading");
});
