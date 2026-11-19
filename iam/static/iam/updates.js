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
  let lastFetch = 0;
  let installTimer = null;
  let slowShown = false;

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

  // {status, body}: status 0 when the app did not answer; body null unless JSON.
  async function call(url, payload) {
    const options = { headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (payload !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.headers["X-CSRFToken"] = root.dataset.csrf;
      options.body = JSON.stringify(payload);
    }
    try {
      const answer = await fetch(url, options);
      const json = (answer.headers.get("Content-Type") || "").includes("application/json");
      return { status: answer.status, body: json ? await answer.json() : null };
    } catch {
      return { status: 0, body: null };
    }
  }

  function state() {
    return logic.deriveState(response, { pending, checking });
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

  function adopt(body) {
    if (body && body.app && body.updater !== undefined) response = body;
  }

  async function refresh() {
    lastFetch = Date.now();
    const answer = await call(urls.statusUrl);
    adopt(answer.body);
    const installing = response && response.updater && response.updater.installing;
    if (!pending && installing) {
      // An installation started elsewhere (automatic, or another tab): wait for it too.
      pending = logic.pendingInstall(installing.version, response, Date.now());
      savePending();
      waitForInstall();
    }
    render();
  }

  async function runCheck({ openAfter }) {
    if (checking) return;
    checking = true;
    actionError = "";
    render();
    const answer = await call(urls.checkUrl, {});
    checking = false;
    adopt(answer.body);
    const error = (answer.body && answer.body.error) || (response && response.updater && response.updater.error);
    render();
    const current = state();
    if (openAfter && logic.hasPopover(current, response)) openPanel();
    else if (openAfter && error) showToast(error, "danger");
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
    pending = logic.pendingInstall(version, response, Date.now());
    savePending();
    render();
    openPanel();
    const answer = await call(urls.installUrl, { version });
    if (answer.status === 202) {
      adopt(answer.body);
      render();
      waitForInstall();
      return;
    }
    // Refused (409, 400) or no update service: nothing installs.
    pending = null;
    savePending();
    adopt(answer.body);
    actionError = (answer.body && answer.body.error) || "The installation could not start.";
    render();
    openPanel();
  }

  function waitForInstall() {
    clearTimeout(installTimer);
    installTimer = setTimeout(pollInstall, INSTALL_POLL_MS);
  }

  async function pollInstall() {
    if (!pending) return;
    const answer = await call(urls.statusUrl);
    if (!answer.body || answer.status >= 500) {
      // The app restarts: expected; keep asking.
      restarting = true;
      pending.outage = true;
      savePending();
      render();
      waitForInstall();
      return;
    }
    restarting = false;
    adopt(answer.body);
    const outcome = logic.installOutcome(response, pending, Date.now());
    if (outcome === "done") {
      const version = pending.version;
      pending = null;
      savePending();
      closePanel();
      render();
      showToast(`Updated to ${version}`);
      setTimeout(() => window.location.reload(), 1500);
      return;
    }
    if (outcome === "failed") {
      pending = null;
      savePending();
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
    if (logic.hasPopover(current, response)) return openPanel();
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
  setInterval(() => {
    if (!pending) refresh();
  }, POLL_MS);

  render();
  if (pending) {
    // A reload during an installation: keep waiting for it.
    pollInstall();
  } else {
    refresh();
  }
})();
