import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { and, eq, gt, lt } from "drizzle-orm";
import type { Context } from "hono";
import { deleteCookie, getCookie, setCookie } from "hono/cookie";
import { config } from "../config";
import { sessions, users, type User } from "../db/schema";
import { getDb } from "../db/client";

/**
 * Browser sessions. The cookie holds a random token; the database keeps its
 * SHA-256 with the user and the expiry (SESSION_HOURS after sign-in; using
 * the app does not extend it). A session counts only while its user is
 * active.
 */

export const SESSION_COOKIE = "iam_session";

const hash = (token: string) => createHash("sha256").update(token).digest("hex");

export function secureCookies(): boolean {
  return config().APP_URL.startsWith("https://");
}

/** Starts a session for the user and sets its cookie. Returns the expiry. */
export async function startSession(c: Context, user: Pick<User, "id">, idToken = ""): Promise<Date> {
  const token = randomBytes(32).toString("base64url");
  const expiresAt = new Date(Date.now() + config().SESSION_HOURS * 3600 * 1000);
  const db = getDb();
  // A new sign-in replaces the session the browser had.
  await endSession(c);
  await db.insert(sessions).values({ id: hash(token), userId: user.id, expiresAt, idToken });
  await db.update(users).set({ lastLogin: new Date() }).where(eq(users.id, user.id));
  await db.delete(sessions).where(lt(sessions.expiresAt, new Date()));
  setCookie(c, SESSION_COOKIE, token, {
    httpOnly: true,
    secure: secureCookies(),
    sameSite: "Lax",
    path: "/",
    expires: expiresAt,
  });
  return expiresAt;
}

/** The signed-in user of the request and the provider's ID token of the session, or null. */
export async function currentSession(c: Context): Promise<{ user: User; idToken: string } | null> {
  const token = getCookie(c, SESSION_COOKIE);
  if (!token || token.length > 200) return null;
  const [row] = await getDb()
    .select({ user: users, idToken: sessions.idToken })
    .from(sessions)
    .innerJoin(users, eq(users.id, sessions.userId))
    .where(and(eq(sessions.id, hash(token)), gt(sessions.expiresAt, new Date()), eq(users.isActive, true)));
  return row ?? null;
}

/** Ends the request's session; returns the provider's ID token it had. */
export async function endSession(c: Context): Promise<string> {
  const token = getCookie(c, SESSION_COOKIE);
  deleteCookie(c, SESSION_COOKIE, { path: "/", secure: secureCookies() });
  if (!token || token.length > 200) return "";
  const [row] = await getDb()
    .delete(sessions)
    .where(eq(sessions.id, hash(token)))
    .returning({ idToken: sessions.idToken });
  return row?.idToken ?? "";
}

// -- signed values (the sign-in's state cookie) ------------------------------------

function signature(value: string): string {
  return createHmac("sha256", `iam-signed:${config().AUTH_SECRET}`).update(value).digest("base64url");
}

export function sign(data: unknown): string {
  const value = Buffer.from(JSON.stringify(data)).toString("base64url");
  return `${value}.${signature(value)}`;
}

export function unsign<T>(signed: string | undefined): T | null {
  if (!signed) return null;
  const dot = signed.lastIndexOf(".");
  if (dot <= 0) return null;
  const value = signed.slice(0, dot);
  const given = Buffer.from(signed.slice(dot + 1));
  const expected = Buffer.from(signature(value));
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) return null;
  try {
    return JSON.parse(Buffer.from(value, "base64url").toString("utf8")) as T;
  } catch {
    return null;
  }
}
