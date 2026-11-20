import { Hono, type Context } from "hono";
import { updateChannelInputSchema, updateInstallSchema } from "@iam/shared/api/updates";
import type { AppEnv } from "../http/env";
import { jsonBody, requireAdmin } from "../http/middleware";
import { askUpdater, type UpdatesAnswer } from "../updates/updater";

/**
 * Updates of the app, for admins in the web app (never agents or service
 * tokens): the status, a check, an installation and the channel, through
 * the Meldgrove Updater (docs/deployment.md, Automatic updates).
 */
const router = new Hono<AppEnv>();

const answer = (c: Context<AppEnv>, result: UpdatesAnswer) => c.json(result.body, result.status);

router.get("/admin/updates", async (c) => {
  requireAdmin(c);
  return answer(c, await askUpdater({ call: "status" }));
});

router.post("/admin/updates/check", async (c) => {
  requireAdmin(c);
  return answer(c, await askUpdater({ call: "check" }));
});

router.post("/admin/updates/install", async (c) => {
  requireAdmin(c);
  const { version } = await jsonBody(c, updateInstallSchema);
  return answer(c, await askUpdater({ call: "install", version }));
});

router.post("/admin/updates/channel", async (c) => {
  requireAdmin(c);
  const { channel } = await jsonBody(c, updateChannelInputSchema);
  return answer(c, await askUpdater({ call: "channel", channel }));
});

export default router;
