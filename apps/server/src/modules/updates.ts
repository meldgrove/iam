import { Hono, type Context } from "hono";
import type { UpdatesResponse } from "@meldgrove/ui/updates";
import { createUpdatesHandler } from "@meldgrove/ui/updates/server";
import { config } from "../config";
import type { AppEnv } from "../http/env";
import { requireRole } from "../http/middleware";
import { logger } from "../logger";
import { record } from "../engine/audit";

/**
 * Updates of the app, for admins in the web app (never agents or service
 * tokens): the status, a check, an installation and the channel. The shared
 * handler of @meldgrove/ui asks the Meldgrove Updater at UPDATER_URL with
 * UPDATER_TOKEN and answers as the update button expects (docs/updates.md).
 * The API's middleware has already refused changes from other sites
 * (sameOrigin). IAM adds the audit events.
 */
const updates = createUpdatesHandler(() => ({
  url: config().UPDATER_URL,
  token: config().UPDATER_TOKEN,
  appVersion: config().APP_VERSION ?? null,
  logger,
}));

const router = new Hono<AppEnv>();

/**
 * The handler's answer with the headers the middleware set before the route
 * (x-request-id): a Response of its own would lose them.
 */
async function answer(c: Context<AppEnv>, pending: Response | Promise<Response>) {
  const response = await pending;
  return c.newResponse(response.body, response);
}

/** The request's JSON body, read before the shared handler takes it; null when it is no JSON object. */
async function bodyOf(request: Request): Promise<Record<string, unknown> | null> {
  const body: unknown = await request
    .clone()
    .json()
    .catch(() => null);
  return typeof body === "object" && body !== null ? (body as Record<string, unknown>) : null;
}

router.get("/admin/updates", (c) => {
  requireRole(c, "admin");
  return answer(c, updates.respond("status"));
});

router.post("/admin/updates/check", (c) => {
  requireRole(c, "admin");
  return answer(c, updates.respond("check"));
});

router.post("/admin/updates/install", async (c) => {
  const user = requireRole(c, "admin");
  const body = await bodyOf(c.req.raw);
  const response = await updates.respond("install", c.req.raw);
  // 202: the updater took the version, which the handler checked.
  if (response.status === 202) {
    const version = String(body?.version);
    await record("updates.install", `Requested the installation of Meldgrove IAM ${version}`, {
      actor: user,
      details: { version },
    });
  }
  return answer(c, response);
});

const CHANNEL_LABELS = { stable: "Stable", preview: "Preview" } as const;

router.post("/admin/updates/channel", async (c) => {
  const user = requireRole(c, "admin");
  const body = await bodyOf(c.req.raw);
  const response = await updates.respond("channel", c.req.raw);
  if (response.status >= 300) return answer(c, response);
  const channel = body?.channel as keyof typeof CHANNEL_LABELS;
  const result = (await response.clone().json()) as UpdatesResponse;
  // Switched only when the updater's status names the chosen channel (as in the Django version).
  if (result.reachable && !result.error && result.updater?.channel !== channel) {
    return c.json<UpdatesResponse>(
      { ...result, error: "The update service did not switch the channel." },
      409,
      { "cache-control": "no-store" },
    );
  }
  if (result.updater?.channel === channel && !result.error) {
    await record("updates.channel", `Switched the update channel to ${CHANNEL_LABELS[channel]}`, {
      actor: user,
      details: { channel },
    });
  }
  return answer(c, response);
});

export default router;
