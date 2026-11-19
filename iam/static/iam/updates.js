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
  const POLL_MS = 5 * 60 * 1000;
  const INSTALL_POLL_MS = 2000;
  const FOCUS_MIN_MS = 60 * 1000;

  let response = null;
  let checking = false;
  let restarting = false;
  let actionError = "";
  let pending = loadPending();
  // The `last` of the installation that the button followed until it failed.
  let failure = null;
  let lastFetch = 0;
  let installTimer = null;
  let reloadTimer = null;
  let pollTimer = null;
  let slowShown = false;
  // Signed out or no admin any more: no more polling on this page.
  let stopped = false;
  // The installation an admin stopped waiting for: not followed again.
  let dismissed = "";

  function loadPending() {
    try {
      const value = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "null");
      return value && typeof value.version === "string" ? value : null;
    } catch {
      return null;
    }
  }

  function savePending() {
    try {
      if (pending) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(pending));
      else sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      // Without storage, a reload during the restart only stops the waiting.
    }
  }

  // {status, body, signedOut}: status 0 when the app did not answer; body
  // null unless JSON; signedOut for a sign-in redirect, 401 or 403.
  async function call(url, payload) {
    const options = { headers: { Accept: "application/json" }, credentials: "same-origin", redirect: "manual" };
    if (payload !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers["X-CSRFToken"] = root.dataset.csrf;
      options.body = JSON.stringify(payload);
    }
    try {
      const answer = await fetch(url, options);
      // These routes redirect only to the sign-in.
      if (answer.type === "opaqueredirect" || answer.status === 401 || answer.status === 403) {
        return { status: answer.status, body: null, signedOut: true };
      }
      const json = (answer.headers.get("Content-Type") || "").includes("application/json");
      return { status: answer.status, body: json ? await answer.json() : null, signedOut: false };
    } catch {
      return { status: 0, body: null, signedOut: false };
    }
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
    if (!pending && installing && dismissed === `${installing.version} ${installing.startedAt}`) return;
    const followed = logic.follow(pending, response, Date.now());
    if (followed !== pending) {
      pending = followed;
      failure = null;
      savePending();
    }
    if (pending && !installTimer && !stopped) waitForInstall();
  }

  // A sign-in redirect, 401 or 403: stop polling; a page load after signing
  // in again keeps waiting for a saved installation.
  function signedOut() {
    stopped = true;
    restarting = false;
    clearTimeout(installTimer);
    installTimer = null;
    actionError = logic.SIGNED_OUT;
    render();
    if (pending) showToast(logic.SIGNED_OUT, "danger");
  }

  async function refresh() {
    if (stopped) return;
    lastFetch = Date.now();
    const answer = await call(urls.statusUrl);
    if (answer.signedOut) return signedOut();
    adopt(answer.body);
    render();
  }

  async function runCheck({ openAfter }) {
    if (checking || stopped) return;
    checking = true;
    actionError = "";
    render();
    const answer = await call(urls.checkUrl, {});
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
    // Remembered before the request: a lost answer must not lose the installation.
    pending = logic.pendingInstall(version, response, Date.now());
    savePending();
    render();
    openPanel();
    const answer = await call(urls.installUrl, { version });
    // A poll meanwhile ended it, or follows another installation.
    if (!pending || pending.version !== version) return render();
    if (answer.body && answer.body.app && answer.body.updater !== undefined) response = answer.body;
    // Accepted, refused (forget it), another installation (follow that one),
    // or unclear (keep it; the status settles it within a minute).
    const result = logic.answerInstall(pending, answer, Date.now());
    pending = result.pending;
    actionError = result.error;
    savePending();
    if (answer.signedOut) stopped = true;
    render();
    openPanel();
    if (pending) waitForInstall();
  }

  function waitForInstall() {
    clearTimeout(installTimer);
    installTimer = setTimeout(pollInstall, INSTALL_POLL_MS);
  }

  function forget() {
    pending = null;
    savePending();
    clearTimeout(installTimer);
    installTimer = null;
    restarting = false;
    slowShown = false;
  }

  function stopWaiting() {
    const installing = response && response.updater && response.updater.installing;
    dismissed = installing ? `${installing.version} ${installing.startedAt}` : "";
    forget();
    closePanel();
    refresh();
  }

  async function pollInstall() {
    installTimer = null;
    if (!pending || stopped) return;
    const answer = await call(urls.statusUrl);
    if (!pending) return;
    if (answer.signedOut) return signedOut();
    if (!answer.body || !answer.body.app || answer.status >= 500) {
      // The app restarts: expected; keep asking.
      restarting = true;
      pending.outage = true;
      savePending();
      render();
      waitForInstall();
      return;
    }
    adopt(answer.body);
    // The updater restarts as well: expected.
    restarting = !(response && response.reachable);
    const outcome = logic.installOutcome(response, pending, Date.now());
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
    if (outcome === "not-started") {
      const reason = pending.reason;
      forget();
      actionError = reason ? `The installation did not start. ${reason}` : "The installation did not start.";
      render();
      openPanel();
      return;
    }
    render();
    if (outcome === "slow" && !slowShown) {
      slowShown = true;
      openPanel();
    }
    waitForInstall();
  }

  button.addEventListener("click", () => {
    if (!panel.hidden) return closePanel();
    const current = state();
    if (logic.hasPopover(current)) return openPanel();
    if (current === "checking") return undefined;
    if (current === "unreachable") return refresh();
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
    clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      if (!pending) refresh();
    }, POLL_MS);
    if (pending) {
      // A reload during an installation: keep waiting for it.
      pollInstall();
    } else {
      refresh();
    }
  }

  // Leaving the page stops every timer, also the reload after an update; a
  // page restored from the back-forward cache starts again.
  window.addEventListener("pagehide", () => {
    clearInterval(pollTimer);
    clearTimeout(installTimer);
    clearTimeout(reloadTimer);
    installTimer = null;
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) start();
  });

  render();
  start();
})();
