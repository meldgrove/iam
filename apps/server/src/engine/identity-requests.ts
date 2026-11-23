import { createHash, randomBytes } from "node:crypto";
import { and, eq, isNull, lt, or, sql } from "drizzle-orm";
import type { z } from "zod";
import type { identityRequestCreateSchema, IdentityRequestView } from "@iam/shared/api/iam";
import { PERSON_TYPE_LABELS } from "@iam/shared/labels";
import { config } from "../config";
import {
  apiKeys,
  identityRequests,
  people,
  teams,
  type ApiKey,
  type IdentityRequest,
  type User,
} from "../db/schema";
import { atomic, onCommit, q } from "../db/tx";
import { AppError, conflict, notFound, validationError } from "../http/errors";
import { record } from "./audit";
import { sendEmail } from "./email";
import { fullName } from "./people";
import { loadSettings, notificationRecipients } from "./settings";
import { logger } from "../logger";

/**
 * Identity requests: other apps ask for a person through the API
 * (/api/integration/v1) with an API key; an approver creates the person,
 * links a person already here, or rejects the request. Only the person
 * types chosen in Admin › Settings can be requested.
 */

// -- API keys --------------------------------------------------------------------------

const KEY_PREFIX = "iam_";
const KEY_PATTERN = /^iam_[A-Za-z0-9_-]{43}$/;

export function hashApiKey(key: string): string {
  return createHash("sha256").update(key).digest("hex");
}

/** A new key (`iam_` and 32 random bytes), the prefix to show, and the hash to store. */
export function newApiKey(): { key: string; prefix: string; hash: string } {
  const key = KEY_PREFIX + randomBytes(32).toString("base64url");
  return { key, prefix: key.slice(0, 12), hash: hashApiKey(key) };
}

/** The key of an `Authorization: Bearer iam_…` header, if it exists and is not revoked. */
export async function apiKeyFrom(header: string | undefined): Promise<ApiKey | null> {
  const match = /^Bearer\s+(\S+)\s*$/i.exec(header ?? "");
  if (!match || !KEY_PATTERN.test(match[1]!)) return null;
  const [key] = await q()
    .select()
    .from(apiKeys)
    .where(and(eq(apiKeys.keyHash, hashApiKey(match[1]!)), isNull(apiKeys.revokedAt)));
  if (!key) return null;
  // At most one write a minute per key.
  await q()
    .update(apiKeys)
    .set({ lastUsedAt: new Date() })
    .where(
      and(
        eq(apiKeys.id, key.id),
        or(isNull(apiKeys.lastUsedAt), lt(apiKeys.lastUsedAt, new Date(Date.now() - 60_000))),
      ),
    );
  return key;
}

/** How the audit log names a key's actions. */
export const keyLabel = (key: Pick<ApiKey, "name">) => `API key "${key.name}"`;

// -- requests --------------------------------------------------------------------------

export const requestName = (request: Pick<IdentityRequest, "firstName" | "lastName">) =>
  `${request.firstName} ${request.lastName}`.trim();

export const requestUrl = (id: number) => `${config().APP_URL.replace(/\/+$/, "")}/requests/${id}`;

/** The request as the sending app reads it. */
export async function requestView(request: IdentityRequest): Promise<IdentityRequestView> {
  const [person] =
    request.personId === null ? [] : await q().select().from(people).where(eq(people.id, request.personId));
  return {
    id: request.id,
    externalId: request.externalId,
    status: request.status,
    createdAt: request.createdAt.toISOString(),
    decidedAt: request.decidedAt ? request.decidedAt.toISOString() : null,
    reason: request.status === "rejected" ? request.decisionNote : "",
    url: requestUrl(request.id),
    person: person
      ? { status: person.status, workEmail: person.workEmail, startDate: person.startDate }
      : null,
  };
}

type CreateInput = z.output<typeof identityRequestCreateSchema>;

/** Lower-case plural of a type for messages, e.g. "interns". */
const typePlural = (type: CreateInput["personType"]) => `${PERSON_TYPE_LABELS[type].toLowerCase()}s`;

/**
 * Stores a request from another app. The same `externalId` again returns
 * the request stored first (`created: false`), whatever the settings now say.
 */
export async function receiveRequest(
  input: CreateInput,
  key: ApiKey,
): Promise<{ request: IdentityRequest; created: boolean }> {
  const existing = async () =>
    (await q().select().from(identityRequests).where(eq(identityRequests.externalId, input.externalId)))[0];
  const first = await existing();
  if (first) return { request: first, created: false };

  const allowed = (await loadSettings()).identityRequestTypes;
  if (allowed.length === 0) {
    throw new AppError(403, "identity_requests_off", "This installation accepts no identity requests.");
  }
  if (!allowed.includes(input.personType)) {
    throw validationError({
      personType: `This installation accepts no identity requests for ${typePlural(input.personType)}.`,
    });
  }
  if (input.teamId !== null) {
    const [team] = await q().select({ id: teams.id }).from(teams).where(eq(teams.id, input.teamId));
    if (!team) {
      throw validationError({
        teamId: "Select a valid choice. That choice is not one of the available choices.",
      });
    }
  }

  return atomic(async () => {
    const [created] = await q()
      .insert(identityRequests)
      .values({
        externalId: input.externalId,
        externalUrl: input.externalUrl,
        requestedByName: input.requestedBy.name,
        requestedByEmail: input.requestedBy.email.toLowerCase(),
        firstName: input.firstName,
        lastName: input.lastName,
        personalEmail: input.personalEmail,
        jobTitle: input.jobTitle,
        personType: input.personType,
        teamId: input.teamId,
        managerEmail: input.managerEmail.toLowerCase(),
        startDate: input.startDate,
        endDate: input.endDate,
        notes: input.notes,
        apiKeyId: key.id,
      })
      .onConflictDoNothing({ target: identityRequests.externalId })
      .returning();
    // Sent twice at once: the other one stored it.
    if (!created) return { request: (await existing())!, created: false };
    await record("identity_request.received", `Received an identity request for ${requestName(created)}`, {
      actorLabel: keyLabel(key),
      target: { type: "identityrequest", id: created.id },
      details: { person_type: created.personType, external_id: created.externalId },
    });
    // Not awaited: a slow mail server must not hold up the sending app's answer.
    onCommit(() => {
      void notifyRequest(created).catch((error: unknown) =>
        logger.warn({ err: error, requestId: created.id }, "identity request email failed"),
      );
    });
    return { request: created, created: true };
  });
}

/** Tells the notification addresses about a new request; best effort. */
async function notifyRequest(request: IdentityRequest): Promise<void> {
  const recipients = notificationRecipients(await loadSettings());
  if (recipients.length === 0) return;
  const lines = [
    `New identity request in Meldgrove IAM: ${requestName(request)} (${PERSON_TYPE_LABELS[request.personType]}).`,
    "",
    `Review it: ${requestUrl(request.id)}`,
  ];
  await sendEmail(recipients, "New identity request in Meldgrove IAM", lines.join("\n"));
}

/** The request, locked for a decision; 404 when unknown. */
async function lockRequest(id: number): Promise<IdentityRequest> {
  const [request] = await q()
    .select()
    .from(identityRequests)
    .where(eq(identityRequests.id, id))
    .for("update");
  if (!request) throw notFound();
  return request;
}

function requireOpen(request: IdentityRequest): void {
  if (request.status !== "open") {
    throw conflict(`This request is already ${request.status}.`, "request_closed");
  }
}

/** The sending app withdraws a request. Cancelling twice is fine; a decided request stays. */
export async function cancelRequest(id: number, key: ApiKey): Promise<IdentityRequest> {
  return atomic(async () => {
    const request = await lockRequest(id);
    if (request.status === "cancelled") return request;
    if (request.status !== "open") {
      throw conflict(
        `This request is already ${request.status}. Ask the people who manage access to change it.`,
        "request_closed",
      );
    }
    const [saved] = await q()
      .update(identityRequests)
      .set({ status: "cancelled", decidedAt: new Date() })
      .where(eq(identityRequests.id, id))
      .returning();
    await record("identity_request.cancelled", `Cancelled the identity request for ${requestName(saved!)}`, {
      actorLabel: keyLabel(key),
      target: { type: "identityrequest", id },
    });
    return saved!;
  });
}

/**
 * Closes an open request with a person: `addPerson` runs in the same
 * transaction, so the request is approved exactly when the person is saved.
 */
export async function approveRequest(
  id: number,
  actor: User,
  person: () => Promise<{ id: number; firstName: string; lastName: string }>,
  linked: boolean,
): Promise<IdentityRequest> {
  return atomic(async () => {
    const request = await lockRequest(id);
    requireOpen(request);
    const chosen = await person();
    const [saved] = await q()
      .update(identityRequests)
      .set({ status: "approved", decidedAt: new Date(), decidedById: actor.id, personId: chosen.id })
      .where(eq(identityRequests.id, id))
      .returning();
    await record(
      linked ? "identity_request.linked" : "identity_request.approved",
      linked
        ? `Linked the identity request for ${requestName(request)} to ${fullName(chosen)}`
        : `Approved the identity request for ${requestName(request)}`,
      { actor, personId: chosen.id, target: { type: "identityrequest", id } },
    );
    return saved!;
  });
}

export async function rejectRequest(id: number, actor: User, reason: string): Promise<IdentityRequest> {
  return atomic(async () => {
    const request = await lockRequest(id);
    requireOpen(request);
    const [saved] = await q()
      .update(identityRequests)
      .set({ status: "rejected", decidedAt: new Date(), decidedById: actor.id, decisionNote: reason })
      .where(eq(identityRequests.id, id))
      .returning();
    await record("identity_request.rejected", `Rejected the identity request for ${requestName(request)}`, {
      actor,
      target: { type: "identityrequest", id },
    });
    return saved!;
  });
}

/** People who may be the person of a request: same personal or work email, or same name. */
export async function possibleMatches(request: IdentityRequest) {
  const email = request.personalEmail.toLowerCase();
  const rows = await q()
    .select()
    .from(people)
    .where(
      or(
        email ? sql`lower(${people.personalEmail}) = ${email}` : undefined,
        email ? sql`lower(${people.workEmail}) = ${email}` : undefined,
        and(
          sql`lower(${people.firstName}) = ${request.firstName.toLowerCase()}`,
          sql`lower(${people.lastName}) = ${request.lastName.toLowerCase()}`,
        ),
      ),
    )
    .orderBy(people.lastName, people.firstName, people.id)
    .limit(10);
  return rows.map((person) => ({
    id: person.id,
    name: fullName(person),
    status: person.status,
    workEmail: person.workEmail,
    reason:
      email && person.personalEmail.toLowerCase() === email
        ? "Same personal email"
        : email && (person.workEmail ?? "").toLowerCase() === email
          ? "Work email is the requested personal email"
          : "Same name",
  }));
}
