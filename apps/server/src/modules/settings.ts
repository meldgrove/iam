import { Hono } from "hono";
import { eq } from "drizzle-orm";
import { settingsInputSchema, type Settings } from "@iam/shared/api/iam";
import { appSettings } from "../db/schema";
import { atomic, q } from "../db/tx";
import type { AppEnv } from "../http/env";
import { jsonBody, requireRole } from "../http/middleware";
import { record } from "../engine/audit";
import { loadSettings } from "../engine/settings";

/** Admin › Settings: lifecycle rules and notifications. */
const router = new Hono<AppEnv>();

const FIELD_NAMES: Record<keyof Settings, string> = {
  organizationName: "organization_name",
  automaticOnboarding: "automatic_onboarding",
  onboardingLeadDays: "onboarding_lead_days",
  automaticOffboarding: "automatic_offboarding",
  removalLimitPerDay: "removal_limit_per_day",
  notificationEmails: "notification_emails",
};

function view(settings: Settings): Settings {
  return {
    organizationName: settings.organizationName,
    automaticOnboarding: settings.automaticOnboarding,
    onboardingLeadDays: settings.onboardingLeadDays,
    automaticOffboarding: settings.automaticOffboarding,
    removalLimitPerDay: settings.removalLimitPerDay,
    notificationEmails: settings.notificationEmails,
  };
}

router.get("/settings", async (c) => {
  requireRole(c, "admin");
  return c.json(view(await loadSettings()));
});

router.put("/settings", async (c) => {
  const user = requireRole(c, "admin");
  const input = await jsonBody(c, settingsInputSchema);
  await atomic(async () => {
    const current = view(await loadSettings());
    const changed = (Object.keys(FIELD_NAMES) as (keyof Settings)[]).filter((key) => current[key] !== input[key]);
    await q().update(appSettings).set(input).where(eq(appSettings.id, 1));
    await record("settings.saved", "Changed settings", {
      actor: user,
      details: { fields: changed.map((key) => FIELD_NAMES[key]).sort() },
    });
  });
  return c.json({ ...view(await loadSettings()), message: "Saved the settings." });
});

export default router;
