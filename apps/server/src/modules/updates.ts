import { Hono, type Context } from "hono";
import { updateChannelInputSchema, updateInstallSchema } from "@iam/shared/api/updates";
import type { AppEnv } from "../http/env";
import { jsonBody, requireRole } from "../http/middleware";
import { record } from "../engine/audit";
import { askUpdater, type UpdatesAnswer } from "../updates/updater";

/**
 * Updates of the app, for admins in the web app (never agents or service
 * tokens): the status, a check, an installation and the channel, through
 * the Meldgrove Updater (docs/deployment.md, Automatic updates).
 */
const router = new Hono<AppEnv>();

const answer = (c: Context<AppEnv>, result: UpdatesAnswer) => c.json(result.body, result.status);

router.get("/admin/updates", async (c) => {
  requireRole(c, "admin");
  return answer(c, await askUpdater({ call: "status" }));
});

router.post("/admin/updates/check", async (c) => {
  requireRole(c, "admin");
  return answer(c, await askUpdater({ call: "check" }));
});

router.post("/admin/updates/install", async (c) => {
  const user = requireRole(c, "admin");
  const { version } = await jsonBody(c, updateInstallSchema);
  const result = await askUpdater({ call: "install", version });
  if (result.status === 202) {
    await record("updates.install", `Requested the installation of Meldgrove IAM ${version}`, {
      actor: user,
      details: { version },
    });
  }
  return answer(c, result);
});

const CHANNEL_LABELS = { stable: "Stable", preview: "Preview" } as const;

router.post("/admin/updates/channel", async (c) => {
  const user = requireRole(c, "admin");
  const { channel } = await jsonBody(c, updateChannelInputSchema);
  const result = await askUpdater({ call: "channel", channel });
  if (result.status < 300 && result.body.updater?.channel === channel && !result.body.error) {
    await record("updates.channel", `Switched the update channel to ${CHANNEL_LABELS[channel]}`, {
      actor: user,
      details: { channel },
    });
  }
  return answer(c, result);
});

export default router;
