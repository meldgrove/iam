import { lt, sql } from "drizzle-orm";
import type { DbOrTx } from "../db/client";
import { auditLog } from "../db/schema";
import type { UserPrincipal } from "../auth/principal";

/** Records who changed or downloaded what. `summary` is shown to admins; never put secrets in it. */
export async function audit(
  db: DbOrTx,
  actor: UserPrincipal | null,
  entry: { action: string; entityType: string; entityId?: string | null; summary: string },
) {
  await db.insert(auditLog).values({
    actorId: actor?.id ?? null,
    actorEmail: actor?.email ?? null,
    action: entry.action,
    entityType: entry.entityType,
    entityId: entry.entityId ?? null,
    summary: entry.summary.slice(0, 500),
  });
}

/** Deletes entries older than `days` days (AUDIT_LOG_DAYS). */
export async function pruneAuditLog(db: DbOrTx, days: number) {
  if (days <= 0) return;
  await db.delete(auditLog).where(lt(auditLog.createdAt, sql`now() - make_interval(days => ${days})`));
}
