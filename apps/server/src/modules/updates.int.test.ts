import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { eq } from "drizzle-orm";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { startFakeUpdater, updaterStatus, type FakeUpdater } from "@meldgrove/ui/updates/testing";
import { overrideConfigForTests, resetConfigForTests } from "../config";
import { getDb } from "../db/client";
import { auditEvents, type User } from "../db/schema";
import { createApp } from "../http/app";
import { createUser, request, resetDatabase, signInWithTestHeader, testApp } from "../test/helpers";

/**
 * Port of tests/test_updates.py: the admin routes in front of the Meldgrove
 * Updater (docs/deployment.md, Automatic updates). How they answer the
 * updater (timeouts, refusals, an updater that is not there) is the shared
 * handler's (@meldgrove/ui/updates/server), tested in that package. Here:
 * who may use them, the origin check, UPDATER_TOKEN and IAM's audit events.
 * The Django version also rendered the update button and the settings
 * section into its pages; here the web app does (@meldgrove/ui), and
 * `/api/v1/me` tells it whether to show the button.
 */

const TOKEN = "updater-test-token-0123456789abcdef";

const ROUTES: [string, string, unknown][] = [
  ["GET", "/api/v1/admin/updates", undefined],
  ["POST", "/api/v1/admin/updates/check", {}],
  ["POST", "/api/v1/admin/updates/install", { version: "0.2.1" }],
  ["POST", "/api/v1/admin/updates/channel", { channel: "preview" }],
];

let updater: FakeUpdater;
let stopTestHeader: () => void;
let admin: User;

const paths = () => updater.calls.map((call) => call.path);
const auditOf = (action: string) => getDb().select().from(auditEvents).where(eq(auditEvents.action, action));

/** As compose.yaml sets it up: the updater at UPDATER_URL requires the app's UPDATER_TOKEN. */
function useUpdater(settings: { UPDATER_URL: string; UPDATER_TOKEN?: string }) {
  process.env.UPDATER_URL = settings.UPDATER_URL;
  if (settings.UPDATER_TOKEN === undefined) delete process.env.UPDATER_TOKEN;
  else process.env.UPDATER_TOKEN = settings.UPDATER_TOKEN;
  resetConfigForTests();
  overrideConfigForTests({ APP_VERSION: undefined });
}

describe("updates API", () => {
  beforeAll(async () => {
    stopTestHeader = signInWithTestHeader();
    updater = await startFakeUpdater({ token: TOKEN });
  });
  afterAll(async () => {
    stopTestHeader();
    await updater.close();
    delete process.env.UPDATER_URL;
    delete process.env.UPDATER_TOKEN;
    resetConfigForTests();
  });
  beforeEach(async () => {
    useUpdater({ UPDATER_URL: updater.url, UPDATER_TOKEN: TOKEN });
    await resetDatabase();
    admin = await createUser("admin");
    updater.calls.length = 0;
    updater.respond(() => ({ body: updaterStatus() }));
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

  it("passes the status through, with UPDATER_TOKEN", async () => {
    overrideConfigForTests({ APP_VERSION: "0.2.0" });
    const response = await request("GET", "/api/v1/admin/updates", {
      user: admin,
      headers: { "x-request-id": "updates-status-0001" },
    });
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    // The API's middleware sets it before the route; the shared handler's Response keeps it.
    expect(response.headers.get("x-request-id")).toBe("updates-status-0001");
    expect(response.body).toEqual({
      app: { version: "0.2.0" },
      reachable: true,
      updater: updaterStatus(),
      error: null,
    });
    expect(updater.calls).toEqual([
      { method: "GET", path: "/v1/status", body: null, authorization: `Bearer ${TOKEN}` },
    ]);
  });

  it("says that UPDATER_TOKEN is missing when the updater requires one", async () => {
    useUpdater({ UPDATER_URL: updater.url });
    const response = await request("GET", "/api/v1/admin/updates", { user: admin });
    expect(response.status).toBe(409);
    expect(response.body).toMatchObject({ reachable: true, error: expect.stringContaining("UPDATER_TOKEN") });
    expect(updater.calls.map((call) => call.authorization)).toEqual([null]);
  });

  it("switches updates off with an empty UPDATER_URL", async () => {
    useUpdater({ UPDATER_URL: "", UPDATER_TOKEN: TOKEN });
    expect((await request("GET", "/api/v1/admin/updates", { user: admin })).body).toEqual({
      app: { version: null },
      reachable: false,
      updater: null,
      error: null,
    });
    expect((await request("GET", "/api/v1/me", { user: admin })).body.updatesButton).toBe(false);
    expect(updater.calls).toEqual([]);
  });

  it.each([["0.2.1"], ["10.20.30-preview.3"], ["sha256:0123456789ab"]])(
    "passes the version %s on and audits",
    async (version) => {
      const installing = updaterStatus({
        installing: { version, startedAt: "2026-11-19T09:00:00Z", manual: true, stage: "requested" },
      });
      updater.respond(() => ({ status: 202, body: installing }));
      const response = await request("POST", "/api/v1/admin/updates/install", {
        user: admin,
        body: { version },
      });
      expect(response.status).toBe(202);
      expect(response.headers.get("x-request-id")).toBeTruthy();
      expect(response.body.updater).toEqual(installing);
      expect(updater.calls).toEqual([
        { method: "POST", path: "/v1/install", body: { version }, authorization: `Bearer ${TOKEN}` },
      ]);
      const [event, ...others] = await auditOf("updates.install");
      expect(others).toEqual([]);
      expect(event!.actorId).toBe(admin.id);
      expect(event!.details).toEqual({ version });
      expect(event!.summary).toBe(`Requested the installation of Meldgrove IAM ${version}`);
    },
  );

  it("installs only versions, and audits only what the updater took", async () => {
    // The shared handler checks the version; the Django version answered 400 too.
    const wrong = await request("POST", "/api/v1/admin/updates/install", {
      user: admin,
      body: { version: "latest" },
    });
    expect(wrong.status).toBe(400);
    expect(updater.calls).toEqual([]);

    updater.respond((call) =>
      call.path === "/v1/install"
        ? { status: 409, body: { error: "0.2.1 is being installed." } }
        : { body: updaterStatus() },
    );
    const refused = await request("POST", "/api/v1/admin/updates/install", {
      user: admin,
      body: { version: "0.2.1" },
    });
    expect(refused.status).toBe(409);
    expect(refused.body.error).toBe("0.2.1 is being installed.");
    expect(await auditOf("updates.install")).toEqual([]);
  });

  it("switches the channel and audits it", async () => {
    updater.respond(() => ({ body: updaterStatus({ channel: "preview", cooldown: null }) }));
    const response = await request("POST", "/api/v1/admin/updates/channel", {
      user: admin,
      body: { channel: "preview" },
    });
    expect(response.status).toBe(200);
    expect(response.headers.get("x-request-id")).toBeTruthy();
    expect(response.body.updater.channel).toBe("preview");
    expect(updater.calls).toEqual([
      { method: "POST", path: "/v1/channel", body: { channel: "preview" }, authorization: `Bearer ${TOKEN}` },
    ]);
    const [event, ...others] = await auditOf("updates.channel");
    expect(others).toEqual([]);
    expect(event!.details).toEqual({ channel: "preview" });
    expect(event!.summary).toBe("Switched the update channel to Preview");
  });

  it("counts a channel as switched only when the status names it", async () => {
    // The updater answered, but its status still names the old channel.
    const response = await request("POST", "/api/v1/admin/updates/channel", {
      user: admin,
      body: { channel: "preview" },
    });
    expect(response.status).toBe(409);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("x-request-id")).toBeTruthy();
    expect(response.body.error).toBe("The update service did not switch the channel.");
    expect(response.body.updater.channel).toBe("stable");
    expect(await auditOf("updates.channel")).toEqual([]);
  });

  it("passes a locked channel on, and takes only JSON with a channel", async () => {
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
    for (const channel of ["", "beta", "Preview", null, ["preview"]]) {
      const response = await request("POST", "/api/v1/admin/updates/channel", {
        user: admin,
        body: { channel },
      });
      expect(response.status, JSON.stringify(channel)).toBe(400);
    }
    expect(updater.calls).toEqual([]);

    const locked = "The platform's image tag latest decides which updates install. Change it there.";
    updater.respond((call) =>
      call.path === "/v1/channel" ? { status: 409, body: { error: locked } } : { body: updaterStatus() },
    );
    const response = await request("POST", "/api/v1/admin/updates/channel", {
      user: admin,
      body: { channel: "preview" },
    });
    expect(response.status).toBe(409);
    expect(response.body.error).toBe(locked);
    expect(await auditOf("updates.channel")).toEqual([]);
  });

  it("takes changes only from the app's own pages", async () => {
    // The Django version checked its CSRF token; this API checks the Origin.
    const install = (headers: Record<string, string>) =>
      testApp().request("/api/v1/admin/updates/install", {
        method: "POST",
        headers: { "content-type": "application/json", "x-test-user": String(admin.id), ...headers },
        body: JSON.stringify({ version: "0.2.1" }),
      });
    expect((await install({})).status).toBe(403);
    expect((await install({ origin: "https://evil.example.invalid" })).status).toBe(403);
    expect((await install({ "sec-fetch-site": "cross-site" })).status).toBe(403);
    expect(updater.calls).toEqual([]);
    updater.respond(() => ({ status: 202, body: updaterStatus() }));
    expect((await install({ origin: "http://localhost:8000" })).status).toBe(202);
    expect(paths()).toEqual(["/v1/install"]);
  });

  it("shows the update button to admins only, without waiting for the updater", async () => {
    // The button's attributes (URL, app name, guide, dates) are the web app's (@meldgrove/ui).
    expect((await request("GET", "/api/v1/me", { user: admin })).body.updatesButton).toBe(true);
    for (const role of ["viewer", "approver"] as const) {
      const user = await createUser(role);
      expect((await request("GET", "/api/v1/me", { user })).body.updatesButton, role).toBe(false);
    }
    expect((await request("GET", "/api/v1/settings", { user: admin })).status).toBe(200);
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
});
