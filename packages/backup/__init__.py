"""Backups of the database and local application artifacts, and verified restores.

A backup is a directory: a `pg_dump` custom-format file, the résumés, receipts
and diagnostics from `storage/`, and a versioned manifest with a checksum for
every file and a row count for every table.

Two things are deliberately kept out of the default backup and said so in the
manifest: browser profiles, which hold live ATS session cookies, and the vault.
Vault *ciphertext* can be included with an explicit flag; the vault *key* is
never written anywhere by this package. A backup restored without its key
restores unreadable credentials, and that is the safe way round.

Verification restores into a separate database and a separate directory and
compares everything against the manifest. It refuses any target that is the
live database or overlaps the live storage, so checking a backup can never
overwrite the installation it came from.
"""
