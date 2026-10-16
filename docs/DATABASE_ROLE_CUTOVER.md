# Database role cutover

Jobrunner's API and workers use `DATABASE_URL`. Alembic, backup-restore
verification, and the `VACUUM FULL` portion of manual posting pruning may use
a separate `MAINTENANCE_DATABASE_URL`, read directly from the command's
environment. The maintenance URL is deliberately absent from `Settings`, so
starting the API or worker from the normal application environment does not
give either process the administrative credential.

Until separate roles are provisioned, Alembic and restore verification fall
back to `DATABASE_URL`. That compatibility path makes this an expansion step:
merging it does not change a running installation.

## Prepare, without changing the database

Choose a private directory outside the repository and outside any provider-managed
Desktop/Documents folder. In it, create two mode-`0600` files:

- a staged runtime file exporting `DATABASE_URL` with a new, non-administrative
  login such as `jobrunner_app`; and
- a maintenance file exporting `MAINTENANCE_DATABASE_URL` with the existing
  owner/admin login.

Do not put either URL on a command line. Source the files into only the shell
used for the cutover, then preview the grants:

```sh
set -a
source "$PRIVATE_ROOT/jobrunner-runtime-next.env"
source "$PRIVATE_ROOT/jobrunner-maintenance.env"
set +a
make database-role deny="jobrunner_test"
```

The preview prints role and database names, never passwords, and changes
nothing. It refuses a runtime URL that names another server/database, reuses
the maintenance identity, omits a password, or points at an elevated/member
role.

The grant boundary allows `CONNECT`, schema `USAGE`, table CRUD, and sequence
use for existing and future objects created by the maintenance role. It removes
database/schema creation and temporary-table privileges. Each database named
with `deny=` has public `CONNECT` removed as well, which is what prevents a
production runtime role from entering the test environment and vice versa.

## Approved maintenance window

These are live operations and are intentionally not performed by the code
change itself:

1. Keep the original application environment unchanged and available for
   rollback. Do not delete or overwrite its credential.
2. Quiesce the API and workers, take a backup, and record table counts.
3. Source the two staged files, run the dry run again, then run
   `make database-role apply=1 deny="jobrunner_test"`.
4. Run Alembic with the maintenance file sourced. Create a backup using the
   runtime role; source the maintenance file for restore verification because
   creating the disposable restore database is privileged.
5. Atomically switch the normal application environment to the runtime URL,
   restart the API and workers, and verify the connected identity, queue
   claim/heartbeat/settlement, state transitions, ledger writes, pruning, and
   audit reads.

For rollback, stop the services, restore the untouched original application
environment, and restart. Leave the new role and hardened grants in place for
inspection; deleting a role or widening public grants is a separate,
explicitly approved operation.
