import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { eq } from "drizzle-orm";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { UpdaterStatus } from "@iam/shared/api/updates";
import { overrideConfigForTests, resetConfigForTests } from "../config";
import { getDb } from "../db/client";
import { auditEvents, type User } from "../db/schema";
import { createApp } from "../http/app";
import { startFakeUpdater, updaterStatus } from "../test/fake-updater";
import { createUser, request, resetDatabase, signInWithTestHeader, testApp } from "../test/helpers";

/**
 * Port of tests/test_updates.py: the admin routes in front of the Meldgrove
 * Updater (docs/updates.md). The Django version also rendered the update
 * button and the settings section into its pages; here the web app does
 * (@meldgrove/ui), and `/api/v1/me` tells it whether to show the button.
 */

const STATUS: UpdaterStatus = updaterStatus({
  available: {
    version: "0.2.1",
    preview: false,
    publishedAt: "2026-11-17T10:00:00Z",
    url: "https://github.com/meldgrove/iam/releases/tag/v0.2.1",
    notes: ["Fix the sign-in loop behind some proxies"],
    changes: 1,
    installsAt: "2026-11-20T02:00:00Z",
    failed: null,
  },
  checkedAt: "2026-11-19T08:00:00Z",
  window: "02:00-05:00",
  timezone: "Europe/Berlin",
});

const ROUTES: [string, string, unknown][] = [
  ["GET", "/api/v1/admin/updates", undefined],
  ["POST", "/api/v1/admin/updates/check", {}],
  ["POST", "/api/v1/admin/updates/install", { version: "0.2.1" }],
  ["POST", "/api/v1/admin/updates/channel", { channel: "preview" }],
];

let updater: Awaited<ReturnType<typeof startFakeUpdater>>;
let stopTestHeader: () => void;
let admin: User;

type Reply = { status?: number; body?: unknown; delayMs?: number };

/** Answers `"METHOD /path"` with its reply, everything else with 200 and STATUS. */
function answer(routes: Record<string, Reply> = {}) {
  updater.respond((call) => routes[`${call.method} ${call.path}`] ?? { body: STATUS });
}

const paths = () => updater.calls.map((call) => call.path);
const auditOf = (action: string) => getDb().select().from(auditEvents).where(eq(auditEvents.action, action));

const timeout = AbortSignal.timeout.bind(AbortSignal);
/** The timeouts each call asked for; they run a hundred times faster here. */
const fastTimeouts = () => vi.spyOn(AbortSignal, "timeout").mockImplementation((ms) => timeout(ms / 100));

describe("updates API", () => {
  beforeAll(async () => {
    stopTestHeader = signInWithTestHeader();
    updater = await startFakeUpdater();
    process.env.UPDATER_URL = updater.url;
    resetConfigForTests();
  });
  afterAll(async () => {
    stopTestHeader();
    await updater.close();
    delete process.env.UPDATER_URL;
    resetConfigForTests();
  });
  beforeEach(async () => {
    resetConfigForTests();
    overrideConfigForTests({ APP_VERSION: undefined });
    await resetDatabase();
    admin = await createUser("admin");
    updater.calls.length = 0;
    answer();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each(["viewer", "approver"] as const)("lets only admins reach the updater (%s)", async (role) => {
    const user = await createUser(role);
    for (const [method, url, body] of ROUTES) {
      expect((await request(method, url, { user, body })).status, url).toBe(403);
    }
    expect(updater.calls).toEqual([]);
  });

  it("asks signed-out requests to sign in", async () => {
    for (const [method, url, body] of ROUTES) {
      expect((await request(method, url, { body })).status, url).toBe(401);
    }
    expect(updater.calls).toEqual([]);
  });

  it("passes the status through", async () => {
    overrideConfigForTests({ APP_VERSION: "0.2.0" });
    const response = await request("GET", "/api/v1/admin/updates", { user: admin });
    expect(response.status).toBe(200);
    expect(response.body).toEqual({
      app: { version: "0.2.0" },
      reachable: true,
      updater: STATUS,
      error: null,
    });
    expect(updater.calls.map((call) => [call.method, call.path])).toEqual([["GET", "/v1/status"]]);
  });

  it("has no app version in development", async () => {
    expect((await request("GET", "/api/v1/admin/updates", { user: admin })).body.app).toEqual({
      version: null,
    });
  });

  it("gives each call its timeout", async () => {
    // The Django version also gave up connecting after 3 s; fetch has no separate connect timeout here.
    const timeouts = fastTimeouts();
    answer({ "POST /v1/install": { status: 202, body: STATUS } });
    for (const [method, url, body] of ROUTES) await request(method, url, { user: admin, body });
    expect(paths()).toEqual(["/v1/status", "/v1/check", "/v1/install", "/v1/channel"]);
    expect(timeouts.mock.calls.map(([ms]) => ms)).toEqual([3_000, 35_000, 10_000, 35_000]);
  });

  it("answers 200 for an updater that is not reachable", async () => {
    overrideConfigForTests({ UPDATER_URL: "http://127.0.0.1:9" });
    for (const [method, url, body] of ROUTES) {
      const response = await request(method, url, { user: admin, body });
      expect(response.status, url).toBe(200);
      expect(response.body, url).toMatchObject({ reachable: false, updater: null });
      // The Django version said "No connection to http://updater:8080."
      expect(response.body.error, url).toBe("The update service is not reachable.");
    }
    expect(await auditOf("updates.install")).toEqual([]);
    expect(await auditOf("updates.channel")).toEqual([]);
  });

  it("counts a timeout as not reachable", async () => {
    fastTimeouts();
    updater.respond(() => ({ delayMs: 1_000, body: STATUS }));
    const response = await request("POST", "/api/v1/admin/updates/check", { user: admin, body: {} });
    expect(response.status).toBe(200);
    expect(response.body.reachable).toBe(false);
    // The Django version said "… within 35 s."
    expect(response.body.error).toBe("The update service did not answer within 35 seconds.");
  });

  it("counts an answer that is no status as not reachable", async () => {
    answer({ "GET /v1/status": { body: "<html>a proxy page</html>" } });
    const response = await request("GET", "/api/v1/admin/updates", { user: admin });
    expect(response.status).toBe(200);
    expect(response.body).toMatchObject({ reachable: false, updater: null });
    expect(response.body.error).toBe("UPDATER_URL answers, but not as an update service.");
  });

  it("switches updates off with an empty UPDATER_URL", async () => {
    overrideConfigForTests({ UPDATER_URL: "" });
    const response = await request("GET", "/api/v1/admin/updates", { user: admin });
    expect(response.status).toBe(200);
    expect(response.body.reachable).toBe(false);
    expect(updater.calls).toEqual([]);
    expect((await request("GET", "/api/v1/me", { user: admin })).body.updatesButton).toBe(false);
  });

  it.each([
    [""],
    ["latest"],
    ["0.2"],
    ["v0.2.1"],
    ["0.2.1-rc.1"],
    ["0.2.1\n"],
    ["sha256:0123"],
    ["0.2.1 && true"],
    [5],
    [null],
  ])("installs only versions (%j)", async (version) => {
    const response = await request("POST", "/api/v1/admin/updates/install", {
      user: admin,
      body: { version },
    });
    // The Django version answered 400 with "Name a version such as 0.2.1." and the current status.
    expect(response.status).toBe(422);
    expect(response.body.error.fields.version).toBeTruthy();
    expect(paths()).not.toContain("/v1/install");
    expect(await auditOf("updates.install")).toEqual([]);
  });

  it.each([["0.2.1"], ["10.20.30-preview.3"], ["sha256:0123456789ab"]])(
    "passes the version %s on and audits",
    async (version) => {
      const installing = updaterStatus({
        ...STATUS,
        installing: { version, startedAt: "2026-11-19T09:00:00Z", manual: true, stage: "requested" },
      });
      answer({ "POST /v1/install": { status: 202, body: installing } });
      const response = await request("POST", "/api/v1/admin/updates/install", {
        user: admin,
        body: { version },
      });
      expect(response.status).toBe(202);
      expect(response.body.updater.installing.version).toBe(version);
      expect(updater.calls.filter((call) => call.path === "/v1/install").map((call) => call.body)).toEqual([
        { version },
      ]);
      const [event, ...others] = await auditOf("updates.install");
      expect(others).toEqual([]);
      expect(event!.actorId).toBe(admin.id);
      expect(event!.details).toEqual({ version });
    },
  );

  it("answers a refusal with 409 and the latest status", async () => {
    answer({ "POST /v1/install": { status: 409, body: { error: "0.2.1 is being installed." } } });
    const response = await request("POST", "/api/v1/admin/updates/install", {
      user: admin,
      body: { version: "0.2.1" },
    });
    expect(response.status).toBe(409);
    expect(response.body).toEqual({
      app: { version: null },
      reachable: true,
      updater: STATUS,
      error: "0.2.1 is being installed.",
    });
    expect(await auditOf("updates.install")).toEqual([]);
  });

  it("explains an updater error without a sentence", async () => {
    answer({ "POST /v1/check": { status: 500, body: "boom" } });
    const response = await request("POST", "/api/v1/admin/updates/check", { user: admin, body: {} });
    expect(response.status).toBe(500);
    // The Django version said "The update service answered HTTP 500."
    expect(response.body.error).toBe("The update service refused (HTTP 500).");
  });

  it("keeps an updater failure on install a 5xx", async () => {
    // The updater may have taken the request: the button must not read it as
    // a refusal (409) and forget the installation.
    answer({ "POST /v1/install": { status: 503, body: { error: "The volume is not writable." } } });
    const response = await request("POST", "/api/v1/admin/updates/install", {
      user: admin,
      body: { version: "0.2.1" },
    });
    expect(response.status).toBe(503);
    expect(response.body).toEqual({
      app: { version: null },
      reachable: true,
      updater: STATUS,
      error: "The volume is not writable.",
    });
    expect(await auditOf("updates.install")).toEqual([]);
  });

  it("switches only to stable or preview", async () => {
    answer({ "POST /v1/channel": { body: STATUS } });
    for (const channel of ["", "beta", "Preview", null, ["preview"]]) {
      const response = await request("POST", "/api/v1/admin/updates/channel", {
        user: admin,
        body: { channel },
      });
      // The Django version answered 400 with "The channel is stable or preview."
      expect(response.status, JSON.stringify(channel)).toBe(422);
      expect(response.body.error.fields.channel).toBeTruthy();
    }
    expect(paths()).not.toContain("/v1/channel");
    const notJson = await testApp().request("/api/v1/admin/updates/install", {
      method: "POST",
      headers: {
        origin: "http://localhost:8000",
        "content-type": "application/json",
        "x-test-user": String(admin.id),
      },
      body: "not json",
    });
    expect(notJson.status).toBe(400);
    expect(paths()).not.toContain("/v1/install");
  });

  it("switches the channel and audits it", async () => {
    answer({ "POST /v1/channel": { body: { ...STATUS, channel: "preview", cooldown: null } } });
    const response = await request("POST", "/api/v1/admin/updates/channel", {
      user: admin,
      body: { channel: "preview" },
    });
    expect(response.status).toBe(200);
    expect(response.body.updater.channel).toBe("preview");
    expect(updater.calls.filter((call) => call.path === "/v1/channel").map((call) => call.body)).toEqual([
      { channel: "preview" },
    ]);
    const [event] = await auditOf("updates.channel");
    expect(event!.details).toEqual({ channel: "preview" });
    expect(event!.summary).toBe("Switched the update channel to Preview");
  });

  it("passes a locked channel on, and takes no form posts", async () => {
    // The Django settings form posted without JavaScript and showed the
    // answer as a message; the web app posts JSON and shows `error`.
    const form = await testApp().request("/api/v1/admin/updates/channel", {
      method: "POST",
      headers: {
        origin: "http://localhost:8000",
        "content-type": "application/x-www-form-urlencoded",
        "x-test-user": String(admin.id),
      },
      body: "channel=preview",
    });
    expect(form.status).toBe(400);
    expect(updater.calls).toEqual([]);

    answer({ "POST /v1/channel": { body: { ...STATUS, channel: "preview" } } });
    expect(
      (await request("POST", "/api/v1/admin/updates/channel", { user: admin, body: { channel: "preview" } }))
        .status,
    ).toBe(200);
    const locked = "The platform's image tag latest decides which updates install. Change it there.";
    answer({ "POST /v1/channel": { status: 409, body: { error: locked } } });
    const response = await request("POST", "/api/v1/admin/updates/channel", {
      user: admin,
      body: { channel: "stable" },
    });
    expect(response.status).toBe(409);
    expect(response.body.error).toBe(locked);
    expect(await auditOf("updates.channel")).toHaveLength(1);
  });

  it("counts a channel as switched only when the status names it", async () => {
    // The updater answered, but its status still names the old channel.
    answer({ "POST /v1/channel": { body: STATUS } });
    const response = await request("POST", "/api/v1/admin/updates/channel", {
      user: admin,
      body: { channel: "preview" },
    });
    expect(response.status).toBe(409);
    expect(response.body.error).toBe("The update service did not switch the channel.");
    expect(response.body.updater.channel).toBe("stable");
    expect(await auditOf("updates.channel")).toEqual([]);
  });

  it("takes changes only from the app's own pages", async () => {
    // The Django version checked its CSRF token; this API checks the Origin.
    const check = (headers: Record<string, string>) =>
      testApp().request("/api/v1/admin/updates/check", {
        method: "POST",
        headers: { "content-type": "application/json", "x-test-user": String(admin.id), ...headers },
        body: "{}",
      });
    expect((await check({})).status).toBe(403);
    expect((await check({ origin: "https://evil.example.invalid" })).status).toBe(403);
    expect((await check({ "sec-fetch-site": "cross-site" })).status).toBe(403);
    expect(updater.calls).toEqual([]);
    expect((await check({ origin: "http://localhost:8000" })).status).toBe(200);
  });

  it("shows the update button to admins only, without waiting for the updater", async () => {
    // The button's attributes (URL, app name, guide, dates) are the web app's (@meldgrove/ui).
    expect((await request("GET", "/api/v1/me", { user: admin })).body.updatesButton).toBe(true);
    for (const role of ["viewer", "approver"] as const) {
      const user = await createUser(role);
      expect((await request("GET", "/api/v1/me", { user })).body.updatesButton, role).toBe(false);
    }
    expect(updater.calls).toEqual([]);
  });

  it("serves the sign-in page and every app page as the one web app document", async () => {
    // The Django test checked the order of theme.js and the shared tokens in
    // each page's head; the web app's single document carries them now.
    const dist = mkdtempSync(path.join(tmpdir(), "iam-web-"));
    const html = '<!doctype html><html><head><link rel="stylesheet" href="/assets/app.css"></head></html>';
    writeFileSync(path.join(dist, "index.html"), html);
    overrideConfigForTests({ WEB_DIST_DIR: dist });
    const app = createApp();
    for (const page of ["/login", "/", "/admin/settings"]) {
      const response = await app.request(page, { headers: { accept: "text/html" } });
      expect(response.status, page).toBe(200);
      expect(await response.text(), page).toBe(html);
    }
  });

  it("reads the settings without waiting for the updater", async () => {
    expect((await request("GET", "/api/v1/settings", { user: admin })).status).toBe(200);
    expect((await request("GET", "/api/v1/me", { user: admin })).body.updatesButton).toBe(true);
    expect(updater.calls).toEqual([]);
  });

  it("still answers the settings' updates section without an updater", async () => {
    // No update button; the section still says that there is no update service.
    overrideConfigForTests({ UPDATER_URL: "" });
    expect((await request("GET", "/api/v1/me", { user: admin })).body.updatesButton).toBe(false);
    expect((await request("GET", "/api/v1/settings", { user: admin })).status).toBe(200);
    expect((await request("GET", "/api/v1/admin/updates", { user: admin })).body).toEqual({
      app: { version: null },
      reachable: false,
      updater: null,
      error: null,
    });
    expect(updater.calls).toEqual([]);
  });
});
