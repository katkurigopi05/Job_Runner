# Proposed follow-up: separate app and maintenance database roles

Prepared 2026-10-15 after verifying audit phases 3 and 4. **Plan only:** no
role, password, environment file, worker process or live record was changed.

## Outcome and scope

Run the API and workers with a dedicated login that can read and update the
application's tables but cannot administer Postgres, create databases or
change the schema. Keep migrations, restore verification and maintenance
under credentials loaded only by those commands. Preserve the existing
database, object ownership, submission ledger and local bind.

This is the remaining database-role item from phase 2. It does not resolve
the separate iCloud storage decision or the five uncertain submissions.

## Existing credential consumers

| Consumer | Current source / behavior | Required migration behavior |
|---|---|---|
| API, workers and ordinary scripts | `packages/core/config.py` exposes `DATABASE_URL` | Use the restricted application role for ordinary reads and writes |
| Alembic | `migrations/env.py` uses the same settings URL | Load maintenance credentials only in the migration command |
| Backup creation | `scripts/backup.py` passes that URL to `pg_dump` | Prove a dump succeeds with the selected backup role and contains all expected tables |
| Backup verification | `packages/backup/verify.py` creates and drops an isolated restore database | Keep its existing target guards; use separately supplied maintenance credentials |
| Manual pruning | `scripts/prune_postings.py` performs `VACUUM FULL` after deletion | Run the maintenance command with maintenance credentials; ordinary automatic pruning must work with app grants |
| Tests | `tests/conftest.py` drops/recreates the marked test schema | Keep a distinct test connection and both live-target refusal and positive-marker checks |
| Fresh installs | `scripts/init-db.sql`, `docker-compose.yml` | Provision app grants without assuming first-init scripts rerun on existing volumes |

The existing superuser owns the objects. Retain that ownership during the
first rollout so rollback does not require moving objects back. Do not give
the app membership in the owner role. Inventory the DAB connection and every
remaining `DATABASE_URL` consumer before implementation is considered complete.

## Implementation sequence

1. Add an explicit maintenance credential path to the commands above. Keep
   existing installs compatible until the separate credentials are configured.
   API and worker processes must not need the maintenance secret. Document
   how fresh installs and existing volumes take the same final configuration.
2. In an isolated database, provision a dedicated app login with no elevated
   role attributes. Grant database connection, schema usage, required table
   operations and sequence access. Inventory views and functions before
   granting privileges. Set future-object defaults for the actual migration
   owner, and prove newly migrated tables are usable by the app role.
3. Prove the complete privilege boundary in that isolated database. Audit
   privileges inherited through `PUBLIC` as well as direct grants. Require
   ordinary queue claims, lease renewal, state transitions, ledger fencing,
   pruning and audit reads to work. Require schema/database creation, owner
   role assumption and privileged server operations to be refused.
4. Produce a concrete rollout command and rollback command with no passwords
   in arguments, output, committed SQL or logs. Role/grant provisioning must
   be repeatable and must report partial setup without silently rotating an
   existing password. Password generation and storage need their own explicit
   step. Use a unique app password; the shipped default is not a new secret.
5. Before live cutover, create and verify a backup, record schema revision
   and counts, and inspect active work. Quiesce writers at a safe point;
   never interrupt a submission click to rotate credentials. Provision the
   role/grants first, update runtime configuration, then restart services
   with the new login. Existing and new clients can coexist during rollout
   because neither schema nor ownership changes.
6. Verify the actual runtime connection identity and absence of elevated
   privileges, API reads, worker heartbeat and backup/restore commands. Use
   fixtures for write-path and submission tests; do not send an employer
   application as a smoke test. Compare revision and authoritative row counts
   with the pre-cutover record, accounting for any intentionally resumed work.

## Rollback and stop condition

Keep the previous configuration available outside the synced tree, with
restricted access. If validation fails, quiesce the affected services,
restore their previous connection configuration and restart them. Since
object ownership and schema are unchanged, rollback needs no data restore.
Leave the added role in place until recovery is verified; dropping roles or
revoking old administrator access is a later explicit operation.

Stop after isolated positive/negative permission tests, migration and backup
proof, and reviewable rollout/rollback commands exist. Applying this plan to
the owner's running database is a separate step. A credential change alone
is insufficient: maintenance commands must continue to work, and their
privileges must not remain available to ordinary runtime processes.
