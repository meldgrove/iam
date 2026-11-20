import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import type pg from "pg";
import { logger } from "../logger";

/**
 * Takes over the database of the Django version (IAM 0.1 and 0.2) in place.
 *
 * The tables, columns, constraints and indexes keep their names; the first
 * migration (0000_django_baseline) creates exactly them in a new database.
 * For a Django database, this brings it to the state of that migration and
 * records the migration as applied, so the migrations after it run as on a
 * new database:
 *
 * - adds the columns of Django migrations 0002 to 0004 when an installation
 *   of 0.1 never got them, and records those migrations for Django;
 * - gives the foreign keys the rules on delete that Django applied itself
 *   (cascade, set null, protect);
 * - drops Django's extra indexes for LIKE searches;
 * - gives `iam_user.password`, which only Django used, an empty default.
 *
 * Django's own tables (sessions, permissions, its migration list) and the
 * password column stay, so going back to the version before still works.
 * Everything runs in one transaction: a failure changes nothing.
 */
export async function adoptDjangoDatabase(client: pg.Client, migrationsFolder: string): Promise<boolean> {
  const { rows } = await client.query<{ iam: boolean; django: boolean; journal: boolean }>(`
    select to_regclass('public.iam_person') is not null as iam,
           to_regclass('public.django_migrations') is not null as django,
           to_regclass('drizzle.__drizzle_migrations') is not null as journal
  `);
  const state = rows[0]!;
  if (state.journal) {
    const applied = await client.query("select 1 from drizzle.__drizzle_migrations limit 1");
    if (applied.rowCount) return false;
  }
  if (!state.iam) return false;
  if (!state.django) {
    throw new Error(
      "The database has IAM tables but neither Django's nor this version's migration records. " +
        "Restore a backup or use an empty database.",
    );
  }
  const django = await client.query<{ name: string }>(
    "select name from django_migrations where app = 'iam' order by name",
  );
  const djangoMigrations = new Set(django.rows.map((row) => row.name));
  if (!djangoMigrations.has("0001_initial")) {
    throw new Error("The Django database has no IAM migrations. Restore a backup or use an empty database.");
  }

  const journal = readJournal(migrationsFolder);
  const baseline = journal.entries[0];
  if (!baseline || baseline.tag !== "0000_django_baseline") {
    throw new Error("The first migration must be 0000_django_baseline.");
  }
  const baselineSql = fs.readFileSync(path.join(migrationsFolder, `${baseline.tag}.sql`), "utf8");
  const statements = baselineSql.split("--> statement-breakpoint").map((statement) => statement.trim());
  const foreignKeys = statements.filter((statement) => /^ALTER TABLE .* FOREIGN KEY/s.test(statement));
  const indexes = statements.filter((statement) => /^CREATE (UNIQUE )?INDEX/.test(statement));

  logger.info("taking over the database of the Django version");
  await client.query("begin");
  try {
    // Django migrations 0002 to 0004 (0.1.x installations).
    await client.query("alter table iam_job add column if not exists claim uuid");
    await client.query(
      "alter table iam_person add column if not exists onboarding_paused boolean not null default false",
    );
    await client.query("alter table iam_person alter column onboarding_paused drop default");
    for (const name of ["0002_job_claim", "0003_person_onboarding_paused", "0004_job_rehearsed"]) {
      if (!djangoMigrations.has(name)) {
        await client.query("insert into django_migrations (app, name, applied) values ('iam', $1, now())", [
          name,
        ]);
      }
    }

    // Django's indexes for LIKE searches; this version has none.
    const like = await client.query<{ name: string }>(`
      select indexname as name from pg_indexes
      where schemaname = 'public' and tablename like 'iam\\_%' and indexname like '%\\_like'
    `);
    for (const { name } of like.rows) await client.query(`drop index "${name}"`);

    // Foreign keys with the rules on delete Django applied in Python.
    for (const statement of foreignKeys) {
      const match = /^ALTER TABLE "([a-z_]+)" ADD CONSTRAINT "([a-z0-9_]+)"/.exec(statement);
      if (!match) throw new Error(`Unexpected statement in the baseline: ${statement.slice(0, 80)}`);
      await client.query(`alter table "${match[1]}" drop constraint if exists "${match[2]}"`);
      await client.query(statement.replace(/;$/, ""));
    }
    // Indexes the baseline has; Django created them under the same names.
    for (const statement of indexes) {
      await client.query(statement.replace(/^CREATE (UNIQUE )?INDEX /, "CREATE $1INDEX IF NOT EXISTS ").replace(/;$/, ""));
    }

    // Only Django wrote passwords (never used: sign-in is through the provider).
    await client.query("alter table iam_user alter column password set default ''");

    await client.query('create schema if not exists "drizzle"');
    await client.query(`
      create table if not exists "drizzle"."__drizzle_migrations" (
        id serial primary key, hash text not null, created_at bigint
      )
    `);
    await client.query('insert into "drizzle"."__drizzle_migrations" (hash, created_at) values ($1, $2)', [
      createHash("sha256").update(baselineSql).digest("hex"),
      baseline.when,
    ]);
    await client.query("commit");
  } catch (error) {
    await client.query("rollback").catch(() => undefined);
    throw error;
  }
  logger.info("took over the database of the Django version");
  return true;
}

type Journal = { entries: { idx: number; when: number; tag: string }[] };

function readJournal(migrationsFolder: string): Journal {
  return JSON.parse(fs.readFileSync(path.join(migrationsFolder, "meta", "_journal.json"), "utf8")) as Journal;
}
