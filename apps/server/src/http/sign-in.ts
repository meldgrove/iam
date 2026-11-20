import { Hono, type Context } from "hono";
import { getCookie, setCookie, deleteCookie } from "hono/cookie";
import { config } from "../config";
import { logger } from "../logger";
import type { AppEnv } from "./env";
import { authorizationUrl, completeSignIn, ProviderUnavailable, type SignInState } from "../auth/oidc";
import { userForClaims, SignInRefused } from "../auth/claims";
import { secureCookies, sign, startSession, unsign } from "../auth/session";

/**
 * The sign-in's redirects: /oidc/authenticate/ sends the browser to the
 * provider, /oidc/callback/ comes back from it (the paths of the Django
 * version, so the provider's client needs no change). Failures land on the
 * sign-in page with a reason: `denied` (no active app user, or the sign-in
 * did not check out) or `unavailable` (the provider cannot be reached).
 */

const STATE_COOKIE = "iam_oidc";
const STATE_SECONDS = 600;
/** Sign-ins a browser may have open at once (several tabs). */
const MAX_STATES = 5;

type StoredState = SignInState & { expires: number };

/** A path in this app to go to after sign-in; anything else becomes "/". */
export function safeNext(value: string | undefined | null): string {
  if (!value || value.length > 2000) return "/";
  const controls = [...value].some((character) => character.charCodeAt(0) < 32);
  if (!value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\") || controls || value.includes("\\")) {
    return "/";
  }
  try {
    const url = new URL(value, "http://app.example.invalid");
    if (url.origin !== "http://app.example.invalid") return "/";
    return url.pathname + url.search + url.hash;
  } catch {
    return "/";
  }
}

function readStates(c: Context): StoredState[] {
  const states = unsign<StoredState[]>(getCookie(c, STATE_COOKIE)) ?? [];
  return Array.isArray(states) ? states.filter((state) => state && state.expires > Date.now()) : [];
}

function writeStates(c: Context, states: StoredState[]) {
  if (states.length === 0) {
    deleteCookie(c, STATE_COOKIE, { path: "/oidc/", secure: secureCookies() });
    return;
  }
  setCookie(c, STATE_COOKIE, sign(states), {
    httpOnly: true,
    secure: secureCookies(),
    sameSite: "Lax",
    path: "/oidc/",
    maxAge: STATE_SECONDS,
  });
}

const toSignIn = (c: Context, error: "denied" | "unavailable") => c.redirect(`/login?error=${error}`, 302);

export function signInRoutes() {
  const app = new Hono<AppEnv>();

  app.get("/oidc/authenticate/", async (c) => {
    if (!config().oidcEnabled) return toSignIn(c, "unavailable");
    try {
      const { url, state } = await authorizationUrl(safeNext(c.req.query("next")));
      const states = [...readStates(c), { ...state, expires: Date.now() + STATE_SECONDS * 1000 }].slice(-MAX_STATES);
      writeStates(c, states);
      c.header("cache-control", "no-store");
      return c.redirect(url, 302);
    } catch (error) {
      logger.warn({ err: error }, "sign-in provider unavailable");
      return toSignIn(c, "unavailable");
    }
  });

  app.get("/oidc/callback/", async (c) => {
    c.header("cache-control", "no-store");
    const states = readStates(c);
    const stateParam = c.req.query("state");
    const state = states.find((item) => item.state === stateParam);
    // Each state is used once.
    writeStates(
      c,
      states.filter((item) => item !== state),
    );
    if (!state || c.req.query("error")) return toSignIn(c, "denied");
    let signedIn: { claims: Record<string, unknown>; idToken: string };
    try {
      const current = new URL(c.req.url);
      const callback = new URL(`${config().APP_URL}/oidc/callback/`);
      // The provider redirected to APP_URL; behind a proxy the request URL may differ.
      current.protocol = callback.protocol;
      current.host = callback.host;
      signedIn = await completeSignIn(current, state);
    } catch (error) {
      if (error instanceof ProviderUnavailable || isNetworkError(error)) {
        logger.warn({ err: error }, "sign-in provider request failed");
        return toSignIn(c, "unavailable");
      }
      logger.warn({ err: error }, "sign-in did not check out");
      return toSignIn(c, "denied");
    }
    try {
      const user = await userForClaims(signedIn.claims);
      if (!user) return toSignIn(c, "denied");
      await startSession(c, user, signedIn.idToken);
    } catch (error) {
      if (error instanceof SignInRefused) {
        logger.warn({ reason: error.message }, "sign-in refused");
        return toSignIn(c, "denied");
      }
      throw error;
    }
    return c.redirect(state.next, 302);
  });

  return app;
}

function isNetworkError(error: unknown): boolean {
  let current: unknown = error;
  for (let depth = 0; depth < 4 && current instanceof Error; depth += 1) {
    if (current.name === "TimeoutError" || current.message === "fetch failed") return true;
    current = current.cause;
  }
  return false;
}
