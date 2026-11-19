// The update button in the top bar, for admins (docs/updates.md). Talks only
// to the app's own /admin/updates/ routes; the logic is in updates-state.js.
"use strict";

(() => {
  const root = document.querySelector("[data-updates]");
  if (!root || typeof IamUpdates === "undefined") return;
  const logic = IamUpdates;
  const urls = root.dataset;
  const app = root.dataset.app || "The app";
  const button = root.querySelector(".updates-button");
  const tip = root.querySelector(".updates-tooltip");
  const panel = root.querySelector(".updates-popover");
  const dialog = document.querySelector(".updates-confirm");
  const toast = document.querySelector(".updates-toast");
  const STORAGE_KEY = "iam.updates.installing";
  const DISMISSED_KEY = "iam.updates.dismissed";
  const POLL_MS = 5 * 60 * 1000;
  const INSTALL_POLL_MS = 2000;
  const FOCUS_MIN_MS = 60 * 1000;
  // How long each request may take; the routes wait for the updater up to
  // 6 s (status), 16 s (install) and 41 s (check).
  const STATUS_REQUEST_MS = 10 * 1000;
  const INSTALL_REQUEST_MS = 25 * 1000;
  const CHECK_REQUEST_MS = 50 * 1000;

  let response = null;
  let checking = false;
  let restarting = false;
  let actionError = "";
  let pending = load(STORAGE_KEY);
  // The `last` of the installation that the button followed until it failed.
  let failure = null;
  let lastFetch = 0;
  let installTimer = null;
  let reloadTimer = null;
  let pollTimer = null;
  // The wait's own timers: the deadline of an unconfirmed attempt and the
  // 15 minutes until Stop waiting, whatever the polls return.
  let deadlineTimer = null;
  let slowTimer = null;
  let slowShown = false;
  // Signed out or no admin any more: no more automatic requests on this page.
  let stopped = false;
  // The installation an admin stopped waiting for: not followed again, also
  // after a reload.
  let dismissed = load(DISMISSED_KEY);

  function load(key) {
    try {
      const value = JSON.parse(sessionStorage.getItem(key) || "null");
      return value && typeof value.version === "string" ? value : null;
    } catch {
      return null;
    }
  }

  function save(key, value) {
    try {
      if (value) sessionStorage.setItem(key, JSON.stringify(value));
      else sessionStorage.removeItem(key);
    } catch {
      // Without storage, a reload during the restart only stops the waiting.
    }
  }

  function savePending() {
    save(STORAGE_KEY, pending);
  }

  // {status, body, signedOut}: status 0 when the app did not answer within
  // `timeout` ms; body null unless readable JSON; signedOut for a sign-in
  // redirect (a redirect, or an HTML page instead of JSON), 401 or 403.
  async function call(url, payload, timeout) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    const options = {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      redirect: "manual",
      signal: controller.signal,
    };
    if (payload !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers["X-CSRFToken"] = root.dataset.csrf;
      options.body = JSON.stringify(payload);
    }
    try {
      const answer = await fetch(url, options);
      const type = answer.headers.get("Content-Type") || "";
      // These routes redirect only to the sign-in.
      if (answer.type === "opaqueredirect" || answer.status === 401 || answer.status === 403 ||
        (answer.status < 400 && type.includes("text/html"))) {
        return { status: answer.status, body: null, signedOut: true };
      }
      let body = null;
      if (type.includes("application/json")) {
        try {
          body = await answer.json();
        } catch {
          // Unreadable: the status code still counts.
        }
      }
      return { status: answer.status, body, signedOut: false };
    } catch {
      return { status: 0, body: null, signedOut: false };
    } finally {
      clearTimeout(timer);
    }
  }

  // A request while an unconfirmed attempt waits gets at most the time left.
  function requestTime(limit) {
    if (!pending || pending.confirmed) return limit;
    return Math.min(limit, logic.deadline(pending) - Date.now());
  }

  function state() {
    return logic.deriveState(response, { pending, checking, failure });
  }

  function render() {
    const current = state();
    const text = logic.tooltip(current, response, pending);
    button.dataset.state = current;
    button.setAttribute("aria-label", text);
    tip.textContent = text;
    if (!panel.hidden) fillPanel(current);
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function releaseNotesLink(url, label) {
    const link = element("a", "", label || "Release notes");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  function fillPanel(current) {
    const model = logic.popover(current, response, {
      pending,
      waiting: Boolean(pending) && !stopped,
      restarting,
      now: Date.now(),
      app,
      checking,
      error: actionError,
      failure,
    });
    const parts = [];
    const head = element("div", "updates-head");
    head.append(element("h2", "", model.title || logic.tooltip(current, response, pending)));
    if (model.preview) head.append(element("span", "badge info", "Preview"));
    parts.push(head);
    if (model.meta) parts.push(element("p", "updates-meta", model.meta));
    if (model.message) parts.push(element("p", "updates-message", model.message));
    for (const line of model.text) parts.push(element("p", "", line));
    if (model.failed) parts.push(element("p", "updates-message", model.failed));
    if (model.notes.length) {
      const list = element("ul", "updates-notes");
      for (const note of model.notes) list.append(element("li", "", note));
      if (model.more) list.append(element("li", "muted", model.more));
      parts.push(list);
    }
    if (model.url) {
      const line = element("p");
      line.append(releaseNotesLink(model.url, model.urlLabel));
      parts.push(line);
    }
    if (model.installs) parts.push(element("p", "muted", model.installs));
    if (model.error) parts.push(element("p", "updates-message", model.error));
    if (model.action) {
      const action = element("button", model.action.primary ? "btn primary" : "btn", model.action.label);
      action.type = "button";
      action.addEventListener("click", () => confirmInstall(model.action));
      parts.push(action);
    }
    if (model.stop) {
      const stop = element("button", "btn", model.stop);
      stop.type = "button";
      stop.addEventListener("click", stopWaiting);
      parts.push(stop);
    }
    const footer = element("div", "updates-footer");
    const checked = element("span", "", model.footer.checked);
    footer.append(element("span", "", model.footer.installed), checked);
    if (!checking && current !== "installing") {
      const again = element("button", "link-button", "Check now");
      again.type = "button";
      again.addEventListener("click", () => runCheck({ openAfter: false }));
      checked.append(" · ", again);
    }
    if (model.footer.checkError) footer.append(element("span", "updates-check-error", model.footer.checkError));
    parts.push(footer);
    panel.replaceChildren(...parts);
  }

  function openPanel() {
    panel.hidden = false;
    button.setAttribute("aria-expanded", "true");
    fillPanel(state());
  }

  function closePanel() {
    panel.hidden = true;
    button.setAttribute("aria-expanded", "false");
    actionError = "";
  }

  function showToast(text, tone) {
    toast.textContent = text;
    toast.dataset.tone = tone || "ok";
    toast.hidden = false;
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => (toast.hidden = true), 4000);
  }

  // Takes a status answer (from a poll, a check or a refusal) and follows an
  // installation that it reports: automatic, from another admin or tab.
  function adopt(body) {
    if (!body || !body.app || body.updater === undefined) return;
    response = body;
    const installing = response.updater && response.updater.installing;
    if (!pending && logic.dismisses(dismissed, installing)) return;
    const followed = logic.follow(pending, response, Date.now());
    if (followed !== pending) {
      pending = followed;
      failure = null;
      savePending();
      armTimers();
    }
    if (pending && !installTimer && !stopped) waitForInstall();
  }

  // A sign-in redirect, 401 or 403: stop every automatic request of the page
  // (polls, interval, focus, back-forward cache); a page load after signing
  // in again keeps waiting for a saved installation.
  function signedOut() {
    stopped = true;
    restarting = false;
    stopTimers();
    actionError = logic.SIGNED_OUT;
    render();
    if (pending) showToast(logic.SIGNED_OUT, "danger");
  }

  async function refresh() {
    if (stopped) return;
    lastFetch = Date.now();
    const answer = await call(urls.statusUrl, undefined, STATUS_REQUEST_MS);
    if (stopped) return;
    if (answer.signedOut) return signedOut();
    adopt(answer.body);
    render();
  }

  async function runCheck({ openAfter }) {
    if (checking || stopped) return;
    checking = true;
    actionError = "";
    render();
    const answer = await call(urls.checkUrl, {}, CHECK_REQUEST_MS);
    checking = false;
    if (answer.signedOut) return signedOut();
    adopt(answer.body);
    // A refused or unreachable check says why; a check that failed at the
    // registry says so instead of "Up to date".
    const refused = (answer.body && answer.body.error) || (answer.body ? "" : "The check did not finish.");
    const failed = response && response.updater && response.updater.error;
    actionError = refused;
    render();
    const current = state();
    if (openAfter && logic.hasPopover(current)) openPanel();
    else if (openAfter && (refused || failed)) showToast(refused || `Last check failed: ${failed}`, "danger");
    else if (openAfter && current === "current") showToast(logic.tooltip(current, response, pending));
  }

  function confirmInstall(action) {
    const confirm = action.confirm;
    dialog.querySelector(".updates-confirm-title").textContent = confirm.title;
    dialog.querySelector(".updates-confirm-text").textContent = confirm.text;
    const notes = dialog.querySelector(".updates-confirm-notes");
    notes.replaceChildren();
    if (confirm.url) notes.append(releaseNotesLink(confirm.url));
    notes.hidden = !confirm.url;
    dialog.querySelector(".updates-confirm-ok").className =
      `btn updates-confirm-ok ${confirm.danger ? "danger solid" : "primary"}`;
    dialog.returnValue = "";
    dialog.onclose = () => {
      if (dialog.returnValue === "install") install(action.version);
      else button.focus();
    };
    dialog.showModal();
  }

  async function install(version) {
    actionError = "";
    failure = null;
    stopped = false;
    dismissed = null;
    save(DISMISSED_KEY, null);
    // Remembered before the request: a lost answer must not lose the
    // installation. Its 60 s deadline starts now.
    pending = logic.pendingInstall(version, response, Date.now());
    savePending();
    armTimers();
    render();
    openPanel();
    const answer = await call(urls.installUrl, { version }, requestTime(INSTALL_REQUEST_MS));
    // The deadline ended it meanwhile.
    if (!pending || pending.version !== version) return render();
    if (answer.body && answer.body.app && answer.body.updater !== undefined) response = answer.body;
    // Accepted, refused (forget it), another installation (follow that one),
    // or unclear (keep it; the status or the deadline settles it).
    const result = logic.answerInstall(pending, answer, Date.now());
    pending = result.pending;
    actionError = result.error;
    savePending();
    armTimers();
    if (answer.signedOut) return signedOut();
    render();
    openPanel();
    if (pending) waitForInstall();
  }

  function waitForInstall() {
    clearTimeout(installTimer);
    installTimer = setTimeout(pollInstall, INSTALL_POLL_MS);
  }

  // Sets the wait's own timers for the current attempt.
  function armTimers() {
    clearTimeout(deadlineTimer);
    clearTimeout(slowTimer);
    deadlineTimer = null;
    slowTimer = null;
    if (!pending || stopped) return;
    const now = Date.now();
    if (!pending.confirmed) deadlineTimer = setTimeout(settleUnconfirmed, Math.max(0, logic.deadline(pending) - now));
    if (!slowShown) slowTimer = setTimeout(showSlow, Math.max(0, pending.startedAt + logic.SLOW_AFTER_MS - now));
  }

  function clearWaitTimers() {
    clearTimeout(installTimer);
    clearTimeout(deadlineTimer);
    clearTimeout(slowTimer);
    installTimer = null;
    deadlineTimer = null;
    slowTimer = null;
  }

  function stopTimers() {
    clearInterval(pollTimer);
    clearWaitTimers();
  }

  function forget() {
    pending = null;
    savePending();
    clearWaitTimers();
    restarting = false;
    slowShown = false;
  }

  // No status confirmed the attempt within 60 s: forget it and say why;
  // normal polling then shows what runs.
  function settleUnconfirmed() {
    deadlineTimer = null;
    if (!pending || pending.confirmed) return;
    const error = logic.unconfirmed(pending);
    forget();
    actionError = error;
    render();
    openPanel();
    refresh();
  }

  function showSlow() {
    slowTimer = null;
    if (!pending || slowShown) return;
    slowShown = true;
    render();
    openPanel();
  }

  // Ignores this installation from then on, by the identity stored on the
  // attempt: the latest status may not report it (unreachable updater).
  function stopWaiting() {
    if (pending) {
      dismissed = logic.dismissal(pending);
      save(DISMISSED_KEY, dismissed);
    }
    forget();
    closePanel();
    refresh();
  }

  async function pollInstall() {
    installTimer = null;
    if (!pending || stopped) return;
    const time = requestTime(STATUS_REQUEST_MS);
    if (time <= 0) return settleUnconfirmed();
    const answer = await call(urls.statusUrl, undefined, time);
    if (!pending || stopped) return;
    if (answer.signedOut) return signedOut();
    // A status follows a reported installation (also another one).
    const body = answer.status < 500 ? answer.body : null;
    adopt(body);
    pending = logic.observe(pending, answer);
    savePending();
    armTimers();
    // No answer, a 5xx or an unreachable updater: the app or the updater
    // restarts, which is expected; keep asking.
    restarting = !(body && body.app && body.reachable && body.updater);
    if (restarting) {
      render();
      waitForInstall();
      return;
    }
    const outcome = logic.installOutcome(response, pending);
    if (outcome === "done") {
      const version = pending.version;
      forget();
      closePanel();
      render();
      showToast(`Updated to ${version}`);
      reloadTimer = setTimeout(() => window.location.reload(), 1500);
      return;
    }
    if (outcome === "failed") {
      failure = response.updater.last;
      forget();
      render();
      openPanel();
      return;
    }
    render();
    waitForInstall();
  }

  button.addEventListener("click", () => {
    if (!panel.hidden) return closePanel();
    const current = state();
    if (logic.hasPopover(current)) return openPanel();
    if (current === "checking") return undefined;
    // Also without an update service: the check says why.
    return runCheck({ openAfter: true });
  });
  document.addEventListener("click", (event) => {
    // The path, not the target: a click can redraw the popover under it.
    if (!panel.hidden && !event.composedPath().includes(root) && !dialog.open) closePanel();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden && !dialog.open) {
      closePanel();
      button.focus();
    }
  });
  window.addEventListener("focus", () => {
    if (!pending && Date.now() - lastFetch >= FOCUS_MIN_MS) refresh();
  });

  function start() {
    if (stopped) return;
    clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      if (!pending) refresh();
    }, POLL_MS);
    if (pending) {
      // A reload during an installation: keep waiting for it.
      armTimers();
      pollInstall();
    } else {
      refresh();
    }
  }

  // Leaving the page stops every timer, also the reload after an update; a
  // page restored from the back-forward cache starts again.
  window.addEventListener("pagehide", () => {
    stopTimers();
    clearTimeout(reloadTimer);
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) start();
  });

  render();
  start();
})();
