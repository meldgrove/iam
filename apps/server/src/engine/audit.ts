import { auditEvents, type AuditEvent, type User } from "../db/schema";
import { q } from "../db/tx";

/** The audit log. Every change by a person or the worker writes one event. */

export const WORKER_LABEL = "Worker";

/** What an event is about, e.g. `{ type: "job", id: 7 }` (the Django model name in lower case). */
export type Target = { type: string; id: number | string };

export async function record(
  action: string,
  summary: string,
  options: {
    actor?: Pick<User, "id" | "email"> | null;
    /** Who acted when it was no app user, e.g. an API key; default: the worker. */
    actorLabel?: string;
    personId?: number | null;
    target?: Target | null;
    details?: Record<string, unknown>;
  } = {},
): Promise<AuditEvent> {
  const actor = options.actor ?? null;
  const [event] = await q()
    .insert(auditEvents)
    .values({
      actorId: actor?.id ?? null,
      actorLabel: (actor ? actor.email : (options.actorLabel ?? WORKER_LABEL)).slice(0, 255),
      action,
      summary: summary.slice(0, 500),
      personId: options.personId ?? null,
      targetType: options.target?.type ?? "",
      targetId: options.target ? String(options.target.id) : "",
      details: options.details ?? {},
    })
    .returning();
  return event!;
}
