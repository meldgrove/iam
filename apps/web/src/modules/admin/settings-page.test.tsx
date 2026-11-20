import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { UpdatesProvider } from "@meldgrove/ui/updates";
import type { Settings } from "@iam/shared/api/iam";
import { renderPage, requests, stubApi } from "@/test-utils";
import { SettingsPage } from "./settings-page";

afterEach(() => vi.unstubAllGlobals());

const settings: Settings = {
  organizationName: "Example",
  automaticOnboarding: true,
  onboardingLeadDays: 7,
  automaticOffboarding: true,
  removalLimitPerDay: 5,
  notificationEmails: "",
};

function renderSettings() {
  return renderPage(
    <UpdatesProvider appName="Meldgrove IAM" project="iam" url="/api/v1/admin/updates">
      <SettingsPage />
    </UpdatesProvider>,
  );
}

describe("settings", () => {
  it("reads the counts as Django's IntegerField does", async () => {
    const fetch = stubApi({
      "GET /settings": { body: settings },
      "PUT /settings": { body: { ...settings, onboardingLeadDays: 10, message: "Saved the settings." } },
    });
    const user = userEvent.setup();
    renderSettings();
    const leadDays = await screen.findByLabelText("Onboarding lead days");
    const limit = screen.getByLabelText("Removal limit per day");
    await user.clear(leadDays);
    await user.type(leadDays, "1e2");
    await user.clear(limit);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(leadDays).toHaveAccessibleDescription(/Enter a whole number\./));
    expect(limit).toHaveAccessibleDescription(/This field is required\./);
    expect(requests(fetch, "PUT /settings")).toHaveLength(0);
    // A trailing ".0" means the whole number.
    await user.clear(leadDays);
    await user.type(leadDays, "10.0");
    await user.type(limit, "3");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved the settings.")).toBeVisible();
    expect(requests(fetch, "PUT /settings")[0]!.body).toMatchObject({
      onboardingLeadDays: 10,
      removalLimitPerDay: 3,
    });
  });
});
