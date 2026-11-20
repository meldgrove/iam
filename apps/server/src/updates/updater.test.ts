import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { resetConfigForTests } from "../config";
import { startFakeUpdater, updaterStatus } from "../test/fake-updater";
import { askUpdater } from "./updater";

let updater: Awaited<ReturnType<typeof startFakeUpdater>>;
const original = { ...process.env };
const timeout = AbortSignal.timeout.bind(AbortSignal);

/** The timeouts each call asked for; they run a hundred times faster here. */
function fastTimeouts() {
  return vi.spyOn(AbortSignal, "timeout").mockImplementation((ms) => timeout(ms / 100));
}

describe("askUpdater", () => {
  beforeAll(async () => {
    updater = await startFakeUpdater();
  });
  afterAll(() => updater.close());

  beforeEach(() => {
    process.env.UPDATER_URL = `${updater.url}/`;
    process.env.APP_VERSION = "0.2.0";
    resetConfigForTests();
    updater.calls.length = 0;
    updater.respond(() => ({ body: updaterStatus() }));
  });
  afterEach(() => {
    process.env = { ...original };
    resetConfigForTests();
    vi.restoreAllMocks();
  });

  it("passes the status on with the app's version", async () => {
    expect(await askUpdater({ call: "status" })).toEqual({
      status: 200,
      body: { app: { version: "0.2.0" }, reachable: true, updater: updaterStatus(), error: null },
    });
    expect(updater.calls).toEqual([{ method: "GET", path: "/v1/status", body: null }]);
  });

  it("sends installs and channels, and answers 202 when an installation started", async () => {
    const installing = updaterStatus({
      installing: {
        version: "0.2.1",
        startedAt: "2026-11-19T09:00:00.000Z",
        manual: true,
        stage: "requested",
      },
    });
    updater.respond(() => ({ status: 202, body: installing }));
    expect(await askUpdater({ call: "install", version: "0.2.1" })).toMatchObject({
      status: 202,
      body: { reachable: true, updater: installing, error: null },
    });
    updater.respond(() => ({ body: updaterStatus({ channel: "preview" }) }));
    expect((await askUpdater({ call: "channel", channel: "preview" })).body.updater?.channel).toBe("preview");
    await askUpdater({ call: "check" });
    expect(updater.calls).toEqual([
      { method: "POST", path: "/v1/install", body: { version: "0.2.1" } },
      { method: "POST", path: "/v1/channel", body: { channel: "preview" } },
      { method: "POST", path: "/v1/check", body: null },
    ]);
  });

  it("answers a refusal with 409, the updater's message and the latest status", async () => {
    updater.respond((call) =>
      call.path === "/v1/install"
        ? { status: 409, body: { error: "0.2.1 is being installed." } }
        : { body: updaterStatus() },
    );
    expect(await askUpdater({ call: "install", version: "0.2.1" })).toEqual({
      status: 409,
      body: {
        app: { version: "0.2.0" },
        reachable: true,
        updater: updaterStatus(),
        error: "0.2.1 is being installed.",
      },
    });
    expect(updater.calls.map((call) => call.path)).toEqual(["/v1/install", "/v1/status"]);

    updater.respond(() => ({ status: 400, body: "not json" }));
    expect(await askUpdater({ call: "channel", channel: "stable" })).toMatchObject({
      status: 409,
      body: { reachable: true, updater: null, error: "The update service refused (HTTP 400)." },
    });
  });

  it("passes an error of the updater (5xx) through with the latest status, never as a refusal", async () => {
    updater.respond((call) =>
      call.path === "/v1/install"
        ? { status: 503, body: { error: "The updater is busy. Try again in a minute." } }
        : { body: updaterStatus() },
    );
    expect(await askUpdater({ call: "install", version: "0.2.1" })).toEqual({
      status: 503,
      body: {
        app: { version: "0.2.0" },
        reachable: true,
        updater: updaterStatus(),
        error: "The updater is busy. Try again in a minute.",
      },
    });
    expect(updater.calls.map((call) => call.path)).toEqual(["/v1/install", "/v1/status"]);
  });

  it("keeps a 202 whose body cannot be read, with the latest status", async () => {
    updater.respond((call) =>
      call.path === "/v1/install" ? { status: 202, body: "accepted" } : { body: updaterStatus() },
    );
    expect(await askUpdater({ call: "install", version: "0.2.1" })).toEqual({
      status: 202,
      body: { app: { version: "0.2.0" }, reachable: true, updater: updaterStatus(), error: null },
    });
  });

  it("refuses an answer that is not a status", async () => {
    updater.respond(() => ({ body: { mode: "everything" } }));
    expect(await askUpdater({ call: "status" })).toMatchObject({
      status: 409,
      body: { reachable: true, updater: null, error: expect.stringMatching(/does not understand/) },
    });
  });

  it("reports an updater that does not answer as not reachable", async () => {
    process.env.UPDATER_URL = "http://127.0.0.1:9";
    resetConfigForTests();
    expect(await askUpdater({ call: "check" })).toEqual({
      status: 200,
      body: {
        app: { version: "0.2.0" },
        reachable: false,
        updater: null,
        error: "The update service is not reachable.",
      },
    });
  });

  it("calls nothing when UPDATER_URL is empty", async () => {
    process.env.UPDATER_URL = "";
    delete process.env.APP_VERSION;
    resetConfigForTests();
    expect(await askUpdater({ call: "status" })).toEqual({
      status: 200,
      body: { app: { version: null }, reachable: false, updater: null, error: null },
    });
    expect(updater.calls).toEqual([]);
  });

  it("gives up after each call's timeout", async () => {
    const timeouts = fastTimeouts();
    updater.respond((call) => (call.path === "/v1/status" ? { delayMs: 300, body: updaterStatus() } : {}));
    expect(await askUpdater({ call: "status" })).toEqual({
      status: 200,
      body: {
        app: { version: "0.2.0" },
        reachable: false,
        updater: null,
        error: "The update service did not answer within 3 seconds.",
      },
    });

    // A change that took too long may have happened: the latest status tells.
    updater.respond((call) => (call.path === "/v1/status" ? { body: updaterStatus() } : { delayMs: 500 }));
    expect(await askUpdater({ call: "install", version: "0.2.1" })).toMatchObject({
      status: 200,
      body: {
        reachable: true,
        updater: updaterStatus(),
        error: "The update service did not answer within 10 seconds.",
      },
    });
    await askUpdater({ call: "check" });
    await askUpdater({ call: "channel", channel: "preview" });
    expect(timeouts.mock.calls.map(([ms]) => ms)).toEqual([
      3_000, 10_000, 3_000, 35_000, 3_000, 35_000, 3_000,
    ]);
  });
});
