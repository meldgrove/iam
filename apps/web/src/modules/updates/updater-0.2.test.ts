import { describe, expect, it } from "vitest";
import { buttonState, failureOf, parseUpdatesResponse } from "@meldgrove/ui/updates/core";
import { updaterStatusSchema, type UpdatesResponse } from "@iam/shared/api/updates";

/** Answers captured from Updater 0.2 (against GHCR and GitHub), shared with the server's tests. */
const samples = import.meta.glob<unknown>("../../../../server/src/test/fixtures/updater-0.2/*.json", {
  eager: true,
  import: "default",
});

function sample(name: string): UpdatesResponse {
  const raw = samples[`../../../../server/src/test/fixtures/updater-0.2/${name}.json`];
  if (raw === undefined) throw new Error(`No sample ${name}`);
  const updater = updaterStatusSchema.parse(raw);
  return { app: { version: updater.installed }, reachable: true, updater, error: null };
}

const stateOf = (name: string) => buttonState(sample(name), { checking: false, pending: null });

describe("the button (@meldgrove/ui) with the real answers of Updater 0.2", () => {
  it("parses every sample, with the app's schema and the button's", () => {
    expect(Object.keys(samples).length).toBeGreaterThanOrEqual(7);
    for (const raw of Object.values(samples)) {
      const updater = updaterStatusSchema.parse(raw);
      const response = { app: { version: updater.installed }, reachable: true, updater, error: null };
      expect(parseUpdatesResponse(response)).not.toBeNull();
    }
  });

  it("shows the update in mode off too (the popover installs nothing there)", () => {
    expect(stateOf("status-off-available-attention")).toBe("available");
  });

  it("offers the update a server installs", () => {
    expect(stateOf("status-host-available")).toBe("available");
  });

  it("is current when the platform decides and offers nothing", () => {
    expect(stateOf("status-platform-locked")).toBe("current");
  });

  it("shows a running installation", () => {
    expect(stateOf("status-installing")).toBe("installing");
    expect(stateOf("install-202")).toBe("installing");
  });

  it("shows a rolled-back installation as failed, also after switching to Preview", () => {
    for (const name of ["status-failed", "status-preview"]) {
      expect(stateOf(name)).toBe("failed");
      expect(failureOf(sample(name))).toMatchObject({
        version: "0.1.3",
        message: "container library-web-1 is unhealthy",
        offer: { version: "0.1.3" },
      });
    }
  });
});
