import { readdirSync, readFileSync } from "node:fs";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { resetConfigForTests } from "../config";
import { startFakeUpdater } from "../test/fake-updater";
import { askUpdater } from "./updater";

/** Answers captured from Updater 0.2 (against GHCR and GitHub). */
const FIXTURES = new URL("../test/fixtures/updater-0.2/", import.meta.url);
const samples = readdirSync(FIXTURES)
  .filter((name) => name.endsWith(".json"))
  .map((name) => ({ name, body: JSON.parse(readFileSync(new URL(name, FIXTURES), "utf8")) as unknown }));

let updater: Awaited<ReturnType<typeof startFakeUpdater>>;
const original = { ...process.env };

describe("the real answers of Updater 0.2", () => {
  beforeAll(async () => {
    updater = await startFakeUpdater();
  });
  afterAll(() => updater.close());

  beforeEach(() => {
    process.env.UPDATER_URL = updater.url;
    process.env.APP_VERSION = "0.1.1";
    resetConfigForTests();
  });
  afterEach(() => {
    process.env = { ...original };
    resetConfigForTests();
  });

  it("has samples", () => {
    expect(samples.length).toBeGreaterThanOrEqual(7);
  });

  it.each(samples)("passes $name on with every field", async ({ name, body }) => {
    const install = name.startsWith("install-");
    updater.respond(() => ({ status: install ? 202 : 200, body }));

    const answer = await askUpdater(install ? { call: "install", version: "0.1.3" } : { call: "status" });

    // toEqual: a field the schema does not know would be missing here.
    expect(answer).toEqual({
      status: install ? 202 : 200,
      body: { app: { version: "0.1.1" }, reachable: true, updater: body, error: null },
    });
  });
});
