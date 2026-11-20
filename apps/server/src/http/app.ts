import { Hono, type Context } from "hono";
import { HTTPException } from "hono/http-exception";
import { secureHeaders } from "hono/secure-headers";
import { bodyLimit } from "hono/body-limit";
import { ZodError } from "zod";
import type { AppEnv } from "./env";
import { AppError, zodFieldErrors } from "./errors";
import { authenticate, requestId, requestLog, sameOrigin } from "./middleware";
import { compression } from "./compression";
import { webApp } from "./static";
import { config } from "../config";
import { API_MODULES } from "../modules";
import health from "../modules/health";
import { signInRoutes } from "./sign-in";
import { logger } from "../logger";
import { errorReportingSources, tracingMiddleware } from "../error-reporting";

/**
 * PostgreSQL errors of a request that ran into another one at the same time:
 * unique violation, deadlock, serialization failure. Trying again gives the
 * right answer.
 */
const CONCURRENT_CHANGE_CODES = new Set(["23505", "40P01", "40001"]);

/** The SQLSTATE of a database error, also when Drizzle wrapped it (`cause`). */
function databaseErrorCode(error: unknown): string | undefined {
  let current = error;
  for (let depth = 0; depth < 3 && current instanceof Error; depth += 1) {
    const code = (current as { code?: unknown }).code;
    if (typeof code === "string") return code;
    current = current.cause;
  }
  return undefined;
}

/** A 409 for a request that ran into a concurrent change; null for other errors. */
export function concurrentChangeResponse(error: Error, c: Context<AppEnv>) {
  const code = databaseErrorCode(error);
  if (!code || !CONCURRENT_CHANGE_CODES.has(code)) return null;
  logger.warn(
    { err: error, requestId: c.get("requestId"), route: `${c.req.method} ${c.req.routePath}`, code },
    "request ran into a concurrent change",
  );
  return c.json(
    {
      error: { code: "concurrent_change", message: "Someone else changed this at the same time. Try again." },
    },
    409,
  );
}

/** The sign-in provider's origin: sign-out may send the browser there. */
function providerOrigin(): string[] {
  try {
    return config().oidcIssuer ? [new URL(config().oidcIssuer).origin] : [];
  } catch {
    return [];
  }
}

export function createApp() {
  const app = new Hono<AppEnv>();
  const tracing = tracingMiddleware(app);
  if (tracing) app.use("*", tracing);
  app.use("*", requestId, requestLog, compression());
  app.use(
    "*",
    secureHeaders({
      contentSecurityPolicy: {
        defaultSrc: ["'self'"],
        scriptSrc: ["'self'"],
        styleSrc: ["'self'", "'unsafe-inline'"],
        imgSrc: ["'self'", "data:", "blob:"],
        fontSrc: ["'self'", "data:"],
        connectSrc: ["'self'", ...errorReportingSources()],
        formAction: ["'self'", ...providerOrigin()],
        frameAncestors: ["'none'"],
        baseUri: ["'self'"],
        objectSrc: ["'none'"],
      },
      referrerPolicy: "same-origin",
      xFrameOptions: "DENY",
      crossOriginEmbedderPolicy: false,
      strictTransportSecurity: config().APP_URL.startsWith("https://")
        ? `max-age=31536000${config().SECURE_HSTS_INCLUDE_SUBDOMAINS ? "; includeSubDomains" : ""}`
        : false,
      permissionsPolicy: { camera: [], microphone: [], geolocation: [] },
    }),
  );
  // Pages and answers can carry personal data: no caching for signed-in users and health checks.
  app.use("*", async (c, next) => {
    await next();
    if (c.req.path.startsWith("/-/") || c.req.raw.headers.get("cookie")?.includes("iam_session=")) {
      if (!c.res.headers.get("cache-control")?.includes("immutable")) c.header("cache-control", "no-store");
    }
  });

  app.onError((error, c) => {
    if (error instanceof AppError) {
      return c.json(
        { error: { code: error.code, message: error.message, fields: error.fields, ...error.extra } },
        error.status,
      );
    }
    if (error instanceof ZodError) {
      return c.json(
        {
          error: {
            code: "validation_error",
            message: "Check the highlighted fields",
            fields: zodFieldErrors(error),
          },
        },
        422,
      );
    }
    if (error instanceof HTTPException) {
      return c.json(
        {
          error: { code: error.status === 413 ? "payload_too_large" : "bad_request", message: error.message },
        },
        error.status,
      );
    }
    const concurrent = concurrentChangeResponse(error, c);
    if (concurrent) return concurrent;
    logger.error(
      { err: error, requestId: c.get("requestId"), route: `${c.req.method} ${c.req.routePath}` },
      "unhandled error",
    );
    return c.json({ error: { code: "internal_error", message: "Something went wrong. Try again." } }, 500);
  });

  app.route("/", health);

  app.route("/", signInRoutes());

  const api = new Hono<AppEnv>();
  // API responses can carry private content.
  api.use("*", async (c, next) => {
    c.header("cache-control", "no-store");
    await next();
  });
  api.use("*", bodyLimit({ maxSize: 2 * 1024 * 1024 }), sameOrigin, authenticate);
  for (const module of API_MODULES) api.route("/", module);
  app.route("/api/v1", api);
  app.all("/api/*", (c) => c.json({ error: { code: "not_found", message: "Not found" } }, 404));

  app.route("/", webApp());
  return app;
}
