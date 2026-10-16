# Private runtime path cutover

Jobrunner can stage its private files outside a provider-managed checkout without changing
the running process or deleting the original copy. The migration copies `storage`, the
encrypted vault, backups, and `.env`; compares every file and retained symlink against a
SHA-256 manifest; rewrites only the copied path settings; and restricts copied directories
to mode `0700` and files to `0600`.

Choose a destination outside Desktop, Documents, iCloud Drive, Dropbox, OneDrive, and
Google Drive. Stop the API, workers, crawler, and browser processes so their marker, lock,
session, and backup files cannot change during the copy. Preview first:

```console
make private-paths root=~/.local/share/jobrunner
```

The preview writes nothing and refuses a synced or overlapping destination. After a current
database backup and during the approved maintenance window, stage the copy:

```console
make private-paths root=~/.local/share/jobrunner apply=1
```

The command publishes the destination only after the copy matches its source manifest. A
failed attempt removes its temporary staging directory. Repeating a successful command
verifies the existing destination; it refuses if either the source or destination changed.
It prints the `JOBRUNNER_ENV_FILE` export needed for the switch, but does not set it.

Export that absolute path in each API, worker, crawler, backup, and maintenance launch
environment. Restart and run `make doctor`. Exercise a read-only API request, confirm the
worker can claim without processing a live submission, verify stored credentials can be
listed without revealing them, and create a fresh backup. A disposable restore remains a
separately authorized operation because it creates a database.

For rollback, stop the processes, unset `JOBRUNNER_ENV_FILE` (or point it at the original
`.env`), and restart. The migration deliberately leaves all original files untouched. Keep
both copies until the read-only smoke checks, worker claim, backup, and separately approved
restore pass. Deleting or retaining the old provider-managed copy is a later owner decision.
