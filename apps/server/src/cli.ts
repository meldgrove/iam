import { eq, ne, sql } from "drizzle-orm";
import { z } from "zod";
import { closeDb } from "./db/client";
import { runMigrations } from "./db/migrate";
import { connections, users } from "./db/schema";
import { atomic, q } from "./db/tx";
import { generateKey, rotate } from "./core/crypto";
import { record } from "./engine/audit";
import { seedDemo } from "./engine/demo";
import { config } from "./config";

/**
 * Commands for operators (docs/deployment.md):
 *   generate-key                          print a new ENCRYPTION_KEY value
 *   create-admin --email <a> [--name <n>] create or reactivate an admin app user
 *   rotate-secrets                        re-encrypt connection secrets with the first ENCRYPTION_KEY
 *   migrate                               apply database migrations
 *   seed-demo [--admin-email <a>]         demo data (development only)
 * In the image: `docker compose run --rm migrate cli <command>`.
 */

function option(args: string[], name: string): string | undefined {
  const index = args.indexOf(name);
  return index === -1 ? undefined : args[index + 1];
}

async function createAdmin(args: string[]) {
  const email = (option(args, "--email") ?? "").trim().toLowerCase();
  const name = option(args, "--name") ?? "";
  if (!z.email().safeParse(email).success) throw new Error("Enter a valid email address.");
  await atomic(async () => {
    const [user] = await q()
      .select()
      .from(users)
      .where(sql`lower(${users.email}) = ${email}`)
      .limit(1);
    let verb: string;
    let id: number;
    if (!user) {
      const [created] = await q()
        .insert(users)
        .values({ email, role: "admin", displayName: name })
        .returning();
      verb = "Created";
      id = created!.id;
    } else {
      await q()
        .update(users)
        .set({ role: "admin", roleSource: "local", isActive: true })
        .where(eq(users.id, user.id));
      verb = "Updated";
      id = user.id;
    }
    await record("user.saved", `${verb} admin ${email} from the command line`, {
      target: { type: "user", id },
    });
    process.stdout.write(`${verb} admin ${email}. Sign in with this address.\n`);
  });
}

async function rotateSecrets() {
  let count = 0;
  await atomic(async () => {
    const rows = await q().select().from(connections).where(ne(connections.secretData, "")).for("update");
    for (const connection of rows) {
      await q()
        .update(connections)
        .set({ secretData: rotate(connection.secretData) })
        .where(eq(connections.id, connection.id));
      count += 1;
    }
  });
  process.stdout.write(`Re-encrypted the secrets of ${count} connection(s).\n`);
}

async function main(args: string[]) {
  const [command, ...rest] = args;
  switch (command) {
    case "generate-key":
      process.stdout.write(`${generateKey()}\n`);
      return;
    case "create-admin":
      return createAdmin(rest);
    case "rotate-secrets":
      return rotateSecrets();
    case "migrate":
      return runMigrations();
    case "seed-demo":
      if (!config().isDevelopment) throw new Error("seed-demo runs only with NODE_ENV=development.");
      await seedDemo(option(rest, "--admin-email") ?? "admin@example.invalid");
      process.stdout.write("Demo data ready.\n");
      return;
    default:
      process.stderr.write(
        "Commands: generate-key, create-admin --email <address> [--name <name>], rotate-secrets, migrate, seed-demo\n",
      );
      process.exitCode = 2;
  }
}

main(process.argv.slice(2))
  .catch((error: unknown) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  })
  .finally(() => closeDb());
