import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { resetConfigForTests } from "../config";
import { startFakeUpdater, updaterStatus } from "../test/fake-updater";
import { createUser, request, resetDatabase } from "../test/helpers";

vi.mock("../auth/resolve", () => import("../test/resolve-mock"));

let updater: Awaited<ReturnType<typeof startFakeUpdater>>;

describe("updates API", () => {
  beforeAll(async () => {
    updater = await startFakeUpdater();
    process.env.UPDATER_URL = updater.url;
    resetConfigForTests();
  });
  afterAll(async () => {
    await updater.close();
    delete process.env.UPDATER_URL;
    resetConfigForTests();
  });
  beforeEach(async () => {
    await resetDatabase();
    updater.calls.length = 0;
    updater.respond(() => ({ body: updaterStatus() }));
  });

  it("is only for admins in the web app", async () => {
    const admin = await createUser({ isAdmin: true });
    const person = await createUser({ isAdmin: false });

    const paths: [string, string, unknown?][] = [
      ["GET", "/api/v1/admin/updates"],
      ["POST", "/api/v1/admin/updates/check", {}],
      ["POST", "/api/v1/admin/updates/install", { version: "0.2.1" }],
      ["POST", "/api/v1/admin/updates/channel", { channel: "preview" }],
    ];
    for (const [method, path, body] of paths) {
      expect((await request(method, path, { body })).status).toBe(401);
      expect((await request(method, path, { userId: person.id, body })).status).toBe(401);
    }
    expect(updater.calls).toEqual([]);

    const status = await request("GET", "/api/v1/admin/updates", { userId: admin.id });
    expect(status.status).toBe(200);
    expect(status.body).toEqual({
      app: { version: null },
      reachable: true,
      updater: updaterStatus(),
      error: null,
    });
  });

  it("protects changes against other sites", async () => {
    const admin = await createUser({ isAdmin: true });
    const response = await request("POST", "/api/v1/admin/updates/install", {
      userId: admin.id,
      body: { version: "0.2.1" },
      headers: { origin: "https://evil.example.net" },
    });
    expect(response.status).toBe(403);
    expect(updater.calls).toEqual([]);
  });

  it("checks versions and channels before it asks the updater", async () => {
    const admin = await createUser({ isAdmin: true });
    for (const version of [
      "latest",
      "0.2",
      "v0.2.1",
      "0.2.1-rc.1",
      "0.2.1\nversion=9.9.9",
      "sha256:abc",
      21,
    ]) {
      const response = await request("POST", "/api/v1/admin/updates/install", {
        userId: admin.id,
        body: { version },
      });
      expect(response.status).toBe(422);
    }
    const channel = await request("POST", "/api/v1/admin/updates/channel", {
      userId: admin.id,
      body: { channel: "nightly" },
    });
    expect(channel.status).toBe(422);
    expect(updater.calls).toEqual([]);

    for (const version of ["0.2.1", "0.2.1-preview.3", "sha256:0123456789ab"]) {
      updater.respond(() => ({ status: 202, body: updaterStatus() }));
      const response = await request("POST", "/api/v1/admin/updates/install", {
        userId: admin.id,
        body: { version },
      });
      expect(response.status).toBe(202);
    }
    expect(updater.calls.map((call) => call.body)).toEqual([
      { version: "0.2.1" },
      { version: "0.2.1-preview.3" },
      { version: "sha256:0123456789ab" },
    ]);
  });

  it("answers the updater's refusal with 409 and the latest status", async () => {
    const admin = await createUser({ isAdmin: true });
    updater.respond((call) =>
      call.path === "/v1/channel"
        ? { status: 409, body: { error: "The platform's image tag latest decides. Change it there." } }
        : { body: updaterStatus({ mode: "platform" }) },
    );
    const response = await request("POST", "/api/v1/admin/updates/channel", {
      userId: admin.id,
      body: { channel: "preview" },
    });
    expect(response.status).toBe(409);
    expect(response.body).toEqual({
      app: { version: null },
      reachable: true,
      updater: updaterStatus({ mode: "platform" }),
      error: "The platform's image tag latest decides. Change it there.",
    });
  });

  it("passes an error of the updater (5xx) through, never as a 409", async () => {
    const admin = await createUser({ isAdmin: true });
    updater.respond((call) =>
      call.path === "/v1/install"
        ? { status: 503, body: { error: "The updater is busy. Try again in a minute." } }
        : { body: updaterStatus() },
    );
    const response = await request("POST", "/api/v1/admin/updates/install", {
      userId: admin.id,
      body: { version: "0.2.1" },
    });
    expect(response.status).toBe(503);
    expect(response.body).toEqual({
      app: { version: null },
      reachable: true,
      updater: updaterStatus(),
      error: "The updater is busy. Try again in a minute.",
    });
  });

  it("reports an updater that is not there", async () => {
    const admin = await createUser({ isAdmin: true });
    process.env.UPDATER_URL = "http://127.0.0.1:9";
    resetConfigForTests();
    try {
      const response = await request("POST", "/api/v1/admin/updates/check", { userId: admin.id, body: {} });
      expect(response.status).toBe(200);
      expect(response.body).toMatchObject({ reachable: false, updater: null, error: expect.any(String) });
    } finally {
      process.env.UPDATER_URL = updater.url;
      resetConfigForTests();
    }
  });
});
