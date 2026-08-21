# scripts/adhoc

One-off analysis scripts, kept as a record of what was actually run rather
than as project tooling. They carry hardcoded candidate ids, absolute paths
into other tools' directories, and read-once input files, so they are excluded
from lint and are not expected to keep working.

`scripts/` proper holds the maintained entry points — `find_boards.py`,
`doctor.py`, `eval_tailor.py`, `record_har.py`.

Written during a parallel Antigravity/Gemini session on 2026-08-20. One of
them, `queue_all_sde.py`, created four real applications against live job
URLs; they are queued and unsent, since nothing submits without approval
(CLAUDE.md §2.3).
