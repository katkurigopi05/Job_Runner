# Phase 2: local network boundary — report

Part of the plan in `docs/HARNESS_AUDIT_2026-10-14.md`, which is added by
PR #164 (Phase 1). Each phase has its own PR based on `main` and its own
report file, so open PRs never edit one shared document.

Done on 2026-10-15. Findings F.1, F.4 (detection only) and F.5 of the audit.

## What changed

| Finding | Before | After | Held by |
|---|---|---|---|
| F.1 Postgres on every interface | `*:5433` (IPv4 and IPv6), superuser `jobrunner`/`jobrunner`, firewall auto-allows signed apps | `127.0.0.1:5433` only. This Mac's LAN address `10.0.0.157:5433` refused; loopback answers | `tests/test_database_binds_loopback.py` (both compose files) |
| F.5 DNS rebinding | API, dashboard `/api` and pages answered `Host: evil.example` with 200 | Dashboard 403, API 401. `localhost` and `127.0.0.1` still 200. A write with a foreign or `null` `Origin` refused before any route | `tests/test_rebinding_guard.py` |
| F.4 private files in iCloud | nothing said so | `make doctor` and `/setup` report "Private files kept off cloud sync" as needing attention, naming which of storage, vault, backups and `.env` are in a synced folder and how to move them. Not blocking | `tests/test_cloud_sync_check.py` |

## What was done to the owner's machine

1. `make backup`: `backups/jobrunner-20261015T075842Z`, 338 MB, 29 tables,
   256,310 rows, revision `b8f4c2d6e901`.
2. `make backup-verify`: checksum, restore, row counts, revision and 221
   artifacts all matched; the scratch database was removed.
3. `docker-compose.override.yml` (the owner's own, uncommitted) changed from
   `"5433:5432"` to `"127.0.0.1:5433:5432"`.
4. `docker compose up -d postgres` recreated the container on the same
   volume. Afterwards: healthy, 5 applications, 5 fences, 12,776 postings,
   168,873 chunks, same revision. The running API reconnected by itself.

The worker pool stayed stopped throughout (stopped 07:42 UTC at the owner's
request).

## Owner decisions still open

- **Where the private files live.** `~/Desktop` carries iCloud Drive's file
  provider mark. iCloud conflict copies of tracked files were dated
  2026-10-13. Either move the checkout (for example to `~/Code/Job_Runner`),
  turn off Desktop & Documents in iCloud Drive, or set `STORAGE_ROOT`,
  `VAULT_ROOT` and `BACKUP_ROOT` outside it. `.env` is read from the folder
  the app starts in, so only moving the checkout covers it. Moving also
  changes the path Claude Code keys its memory on.
- **A non-superuser role and a real password.** Not done here. It changes
  `.env`, which this session cannot write, and it needs a migration plan for
  ownership of existing objects. With the port on loopback it is defense in
  depth, not an open door.

## Not covered

- A phone reaching the dashboard by an mDNS name (`*.local`) is refused. Use
  the address.
- The guard trusts that the browser sets `Host`. A non-browser client on this
  machine can send anything, which is the existing loopback trust, unchanged.
