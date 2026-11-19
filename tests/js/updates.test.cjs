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
  // Without an active wait (none, or polling stopped), no reload is promised.
  const watching = logic.popover("installing", installing, { now: NOW, app: "Meldgrove IAM" });
  assert.deepEqual(watching.text, ["Meldgrove IAM restarts."]);
  assert.equal(logic.popover("installing", installing, { pending, waiting: false, now: NOW + 16 * 60_000 }).stop, "");
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

  // 202 confirms without a readable body; any 4xx refuses; a 5xx that the
  // route passes through is unclear.
  assert.equal(logic.answerInstall(pending, { status: 202, body: null }, NOW).pending.confirmed, true);
  assert.deepEqual(logic.answerInstall(pending, { status: 400, body: null }, NOW), { pending: null, error: "The server answered HTTP 400." });
  const failing = logic.answerInstall(pending, { status: 503, body: { ...before, error: "The volume is not writable." } }, NOW);
  assert.equal(failing.pending.confirmed, false);
  assert.equal(failing.pending.reason, "The volume is not writable.");
});

test("an unclear request ends at its deadline with what the polls saw", () => {
  const before = response({ available: release("0.2.1") });
  const unclear = logic.answerInstall(logic.pendingInstall("0.2.1", before, NOW), { status: 502, body: null }, NOW + 1000).pending;
  assert.equal(logic.deadline(unclear), NOW + 60_000);
  assert.equal(logic.unconfirmed(unclear), "The installation could not be confirmed. The server answered HTTP 502.");
  // Every poll fails: an outage, and the deadline says so.
  const failing = logic.observe(unclear, { status: 0, body: null });
  assert.equal(failing.outage, true);
  assert.equal(logic.unconfirmed(failing), "The installation could not be confirmed. No answer from the server.");
  const unreachable = { app: { version: "0.2.0" }, reachable: false, updater: null, error: "No connection to http://updater:8080." };
  assert.equal(logic.unconfirmed(logic.observe(unclear, { status: 200, body: unreachable })),
    "The installation could not be confirmed. No connection to http://updater:8080.");
  // A reachable status without the attempt, also after an outage.
  const answered = logic.observe(failing, { status: 200, body: before });
  assert.equal(answered.answered, true);
  assert.equal(logic.unconfirmed(answered), "The installation did not start. The server answered HTTP 502.");
  assert.equal(logic.installOutcome(before, answered), "waiting");
  // The status reports it: follow it, and the deadline no longer applies.
  const running = response({ available: release("0.2.1"), installing: { version: "0.2.1", stage: "pulling", startedAt: hoursAgo(0), manual: true } });
  const followed = logic.follow(failing, running, NOW + 2000);
  assert.equal(followed.confirmed, true);
  assert.equal(logic.observe(followed, { status: 200, body: running }), followed);
  // Or a fresh result for it.
  const last = { version: "0.2.1", result: "installed", message: null, finishedAt: new Date(NOW + 5000).toISOString() };
  assert.equal(logic.follow(unclear, response({ last }), NOW + 6000).confirmed, true);
  assert.equal(logic.installOutcome(response({ last }), unclear), "done");
});

test("a result in the same second as the request is fresh (the updater writes whole seconds)", () => {
  const before = response({ mode: "platform", available: release("0.2.1") });
  const pending = logic.answerInstall(logic.pendingInstall("0.2.1", before, NOW + 400), { status: 0, body: null }, NOW + 900).pending;
  const accepted = { version: "0.2.1", result: "accepted", message: null, finishedAt: "2026-11-19T12:00:00Z" };
  const answer = response({ mode: "platform", last: accepted }, { version: "0.2.1" });
  const followed = logic.follow(pending, answer, NOW + 2000);
  assert.equal(followed.confirmed, true);
  assert.equal(logic.installOutcome(answer, followed), "done");
});

test("a retry in the same second does not take the earlier failure as its own", () => {
  const failed = { version: "0.2.1", result: "failed", message: "The deploy link answered HTTP 500.", finishedAt: "2026-11-19T12:00:00Z" };
  const before = response({ mode: "platform", available: release("0.2.1"), last: failed });
  const pending = logic.pendingInstall("0.2.1", before, NOW + 700);
  assert.deepEqual(pending.earlier, { version: "0.2.1", result: "failed", finishedAt: "2026-11-19T12:00:00Z" });
  assert.equal(logic.follow(pending, before, NOW + 2000).confirmed, false);
  const confirmed = { ...pending, confirmed: true };
  assert.equal(logic.installOutcome(before, confirmed), "waiting");
  const again = { ...failed, message: "The deploy link answered HTTP 502.", finishedAt: "2026-11-19T12:00:01Z" };
  assert.equal(logic.installOutcome(response({ mode: "platform", available: release("0.2.1"), last: again }), confirmed), "failed");
});

test("an unreachable updater while following is an outage; a reachable accepted then completes", () => {
  const before = response({ mode: "platform", available: release("0.2.1") }, { version: null });
  // 202 confirms, also when its body cannot be read.
  let pending = logic.answerInstall(logic.pendingInstall("0.2.1", before, NOW), { status: 202, body: null }, NOW + 500).pending;
  assert.equal(pending.confirmed, true);
  const accepted = { version: "0.2.1", result: "accepted", message: null, finishedAt: "2026-11-19T12:00:01Z" };
  const back = response({ mode: "platform", last: accepted }, { version: null });
  assert.equal(logic.installOutcome(back, pending), "waiting", "no outage seen yet");
  const unreachable = { app: { version: null }, reachable: false, updater: null, error: "No connection to http://updater:8080." };
  pending = logic.observe(pending, { status: 200, body: unreachable });
  assert.equal(pending.outage, true);
  pending = logic.observe(pending, { status: 200, body: back });
  assert.equal(logic.installOutcome(back, pending), "done");
});

test("Stop waiting ignores the installation by the identity stored on the attempt", () => {
  const before = response({ available: release("0.2.1") });
  const installing = { version: "0.2.1", stage: "pulling", startedAt: "2026-11-19T12:00:01Z", manual: true };
  const running = response({ available: release("0.2.1"), installing });
  const pending = logic.answerInstall(logic.pendingInstall("0.2.1", before, NOW), { status: 202, body: running }, NOW).pending;
  assert.equal(pending.serverStartedAt, Date.parse(installing.startedAt));
  // The latest status is unreachable when the admin stops waiting: the
  // attempt still knows the installation.
  const dismissed = logic.dismissal(pending);
  assert.equal(logic.dismisses(dismissed, installing), true);
  assert.equal(logic.dismisses(dismissed, { ...installing, startedAt: "2026-11-19T12:30:00Z" }), false, "a later installation");
  assert.equal(logic.dismisses(dismissed, { ...installing, version: "0.2.2" }), false);
  assert.equal(logic.dismisses(null, installing), false);
  // Never reported: the version and the attempt's start.
  const unseen = logic.dismissal(logic.answerInstall(logic.pendingInstall("0.2.1", before, NOW + 500), { status: 202, body: null }, NOW + 600).pending);
  assert.equal(logic.dismisses(unseen, installing), true);
  assert.equal(logic.dismisses(unseen, { ...installing, startedAt: "2026-11-19T11:59:59Z" }), false);
});

test("a newer offer replaces an old failure and keeps its message", () => {
  const last = { version: "0.2.1", result: "rolled-back", message: "migrate failed. 0.2.0 runs again.", finishedAt: hoursAgo(0) };
  const answer = response({ available: release("0.2.2"), last });
  assert.equal(logic.deriveState(answer, { failure: last }), "available");
  const model = logic.popover("available", answer, { now: NOW, failure: last });
  assert.equal(model.title, "Update available");
  assert.equal(model.failed, "0.2.1 failed: migrate failed. 0.2.0 runs again.");
  assert.equal(model.action.version, "0.2.2");
  const attention = response({ attention: release("0.3.0"), last });
  assert.equal(logic.deriveState(attention, { failure: last }), "attention");
  assert.equal(logic.popover("attention", attention, { now: NOW, failure: last }).failed, "0.2.1 failed: migrate failed. 0.2.0 runs again.");
  // While the failed version is still offered, the failure comes first.
  const still = response({ available: release("0.2.1"), attention: release("0.3.0"), last });
  assert.equal(logic.deriveState(still, { failure: last }), "failed");
  assert.equal(logic.popover("available", response({ available: release("0.2.2") }), { now: NOW }).failed, "");
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

// The page script (iam/static/iam/updates.js) with a small fake page, a fake
// fetch and fake timers: the wait's own timers, whatever the requests do.
const SCRIPT = fs.readFileSync(path.join(__dirname, "..", "..", "iam", "static", "iam", "updates.js"), "utf8");
const STORAGE_KEY = "iam.updates.installing";

class FakeNode {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.parts = {};
    this.hidden = false;
    this.className = "";
    this.own = "";
  }

  get textContent() {
    return this.own + this.children.map((child) => (typeof child === "string" ? child : child.textContent)).join(" ");
  }

  set textContent(value) {
    this.own = String(value);
    this.children = [];
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    this.own = "";
    this.children = nodes;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }

  dispatch(type, event = {}) {
    for (const listener of this.listeners[type] || []) listener(event);
  }

  click() {
    this.dispatch("click");
  }

  focus() {}

  querySelector(selector) {
    return this.parts[selector] || null;
  }

  // The button with this label, anywhere below.
  find(label) {
    for (const child of this.children) {
      if (typeof child === "string") continue;
      if (child.tagName === "BUTTON" && child.textContent === label) return child;
      const found = child.find(label);
      if (found) return found;
    }
    return null;
  }
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

// Moves the fake clock in small steps, letting answers arrive in between.
async function advance(t, ms) {
  for (let done = 0; done < ms; done += 250) {
    t.mock.timers.tick(Math.min(250, ms - done));
    await flush();
  }
}

// handle(request) answers {status, body, type}, an Error (no answer) or
// "hang" (no answer until the request is aborted). `storage`: the tab's
// sessionStorage, to load the page again.
function openPage(t, handle, storage = new Map()) {
  if (!storage.size) t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now: NOW });
  const root = new FakeNode();
  root.dataset = { app: "Meldgrove IAM", csrf: "token", statusUrl: "/status", checkUrl: "/check", installUrl: "/install" };
  const button = new FakeNode("button");
  const panel = new FakeNode();
  panel.hidden = true;
  root.parts = { ".updates-button": button, ".updates-tooltip": new FakeNode("span"), ".updates-popover": panel };
  const dialog = new FakeNode("dialog");
  for (const name of ["title", "text", "notes", "ok"]) dialog.parts[`.updates-confirm-${name}`] = new FakeNode();
  dialog.showModal = () => (dialog.open = true);
  const toast = new FakeNode();
  const document = new FakeNode("document");
  document.parts = { "[data-updates]": root, ".updates-confirm": dialog, ".updates-toast": toast };
  document.createElement = (tag) => new FakeNode(tag);
  const window = new FakeNode("window");
  window.reloads = 0;
  window.location = { reload: () => window.reloads++ };
  const sessionStorage = {
    getItem: (key) => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  };
  const requests = [];
  async function fetch(url, options) {
    requests.push({ url, method: options.method || "GET", at: Date.now() });
    const reply = handle({ url, method: options.method || "GET" });
    if (reply === "hang") {
      return new Promise((_, reject) => options.signal.addEventListener("abort", () => reject(new Error("aborted"))));
    }
    if (reply instanceof Error) throw reply;
    const { status = 200, body = null, type = "application/json" } = reply;
    return { type: "basic", status, headers: { get: () => type }, json: async () => body };
  }
  new Function("document", "window", "sessionStorage", "fetch", "IamUpdates", SCRIPT)(document, window, sessionStorage, fetch, logic);
  return { button, panel, dialog, toast, window, storage, requests };
}

// Install now, confirmed in the dialog.
async function installNow(page) {
  page.button.click();
  page.panel.find("Install now").click();
  page.dialog.returnValue = "install";
  page.dialog.onclose();
  await flush();
}

const offer = response({ available: release("0.2.1") });

test("page: the deadline ends an unconfirmed attempt while every request stalls", async (t) => {
  let stalled = false;
  const page = openPage(t, (request) => (stalled || request.method === "POST" ? "hang" : { body: offer }));
  await flush();
  stalled = true;
  await installNow(page);
  await advance(t, 59_750);
  assert.equal(page.storage.has(STORAGE_KEY), true, "still waiting");
  assert.match(page.panel.textContent, /Installing 0\.2\.1…/);
  // The install request was bounded (25 s), then polls every 2 s (10 s each).
  assert.ok(page.requests.filter((request) => request.method === "GET").length >= 3);
  await advance(t, 250);
  assert.equal(page.storage.has(STORAGE_KEY), false);
  assert.match(page.panel.textContent, /The installation could not be confirmed\. No answer from the server\./);
});

test("page: the deadline says the installation did not start when the status answers without it", async (t) => {
  const page = openPage(t, (request) => (request.method === "POST" ? { status: 502, type: "text/html" } : { body: offer }));
  await flush();
  await installNow(page);
  await advance(t, 59_750);
  assert.equal(page.storage.has(STORAGE_KEY), true);
  await advance(t, 250);
  assert.equal(page.storage.has(STORAGE_KEY), false);
  assert.match(page.panel.textContent, /The installation did not start\. The server answered HTTP 502\./);
});

test("page: a sign-in redirect stops every automatic request and keeps the attempt", async (t) => {
  const installing = { version: "0.2.1", stage: "pulling", startedAt: "2026-11-19T12:00:00Z", manual: true };
  const running = response({ available: release("0.2.1"), installing });
  let phase = "offer";
  const page = openPage(t, (request) => {
    if (request.method === "POST") return { status: 202, body: running };
    if (phase === "signed-out") return { body: "<html>Sign in</html>", type: "text/html; charset=utf-8" };
    return { body: { offer, running }[phase] };
  });
  await flush();
  phase = "running";
  await installNow(page);
  await advance(t, 2000);
  phase = "signed-out";
  await advance(t, 2000);
  const seen = page.requests.length;
  await advance(t, 6 * 60_000);
  page.window.dispatch("focus");
  page.window.dispatch("pageshow", { persisted: true });
  await advance(t, 10_000);
  assert.equal(page.requests.length, seen, "no request after the sign-in redirect");
  assert.equal(JSON.parse(page.storage.get(STORAGE_KEY)).version, "0.2.1", "the attempt is kept");
  assert.match(page.panel.textContent, /Sign in again\./);
  assert.doesNotMatch(page.panel.textContent, /reloads/);
});

test("page: Stop waiting while the updater is unreachable ignores that installation when it returns", async (t) => {
  const installing = { version: "0.2.1", stage: "pulling", startedAt: "2026-11-19T12:00:00Z", manual: true };
  const running = response({ available: release("0.2.1"), installing });
  const unreachable = { app: { version: "0.2.0" }, reachable: false, updater: null, error: "No connection to http://updater:8080." };
  let phase = "offer";
  const page = openPage(t, (request) => {
    if (request.method === "POST") return { status: 202, body: running };
    return { body: { offer, running, unreachable }[phase] };
  });
  await flush();
  phase = "running";
  await installNow(page);
  await advance(t, 2000);
  phase = "unreachable";
  // The 15 minutes run on their own timer.
  await advance(t, 15 * 60_000 - 2000);
  const stop = page.panel.find("Stop waiting");
  assert.ok(stop, "Stop waiting after 15 minutes");
  stop.click();
  await flush();
  assert.equal(page.storage.has(STORAGE_KEY), false);
  phase = "running";
  await advance(t, 5 * 60_000);
  const since = page.requests.length;
  await advance(t, 10_000);
  assert.equal(page.requests.length, since, "no install polls every 2 s");
  assert.equal(page.storage.has(STORAGE_KEY), false, "not followed again");
  assert.equal(page.button.dataset.state, "installing");
  page.button.click();
  assert.doesNotMatch(page.panel.textContent, /reloads/);
  // Also after a reload of the page.
  const reloaded = openPage(t, () => ({ body: running }), page.storage);
  await advance(t, 10_000);
  assert.equal(reloaded.requests.length, 1);
  assert.equal(reloaded.storage.has(STORAGE_KEY), false);
});
