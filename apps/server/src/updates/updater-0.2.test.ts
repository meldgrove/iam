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

  it("has every sample", () => {
    expect(samples.map((sample) => sample.name)).toHaveLength(7);
  });

  it.each(samples)("passes $name on as the status, with every field", async ({ body }) => {
    updater.respond(() => ({ body }));

    // toEqual: a field the schema does not know would be missing here.
    expect(await askUpdater({ call: "status" })).toEqual({
      status: 200,
      body: { app: { version: "0.1.1" }, reachable: true, updater: body, error: null },
    });
  });

  it("keeps the 202 of an installation", async () => {
    const { body } = samples.find((sample) => sample.name === "install-202.json")!;
    updater.respond((call) => ({ status: call.path === "/v1/install" ? 202 : 200, body }));

    expect(await askUpdater({ call: "install", version: "0.1.3" })).toEqual({
      status: 202,
      body: { app: { version: "0.1.1" }, reachable: true, updater: body, error: null },
    });
  });
});
