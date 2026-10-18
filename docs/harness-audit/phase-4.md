# Phase 4: audit attribution and derived-data versioning — report

Part of the plan in `docs/HARNESS_AUDIT_2026-10-14.md` (added by PR #164).
Based on `main`, independent of the other phase PRs. Done on 2026-10-15.
Findings F.7, F.12 and F.13 of the audit.

## What changed

| Finding | Before | After | Held by |
|---|---|---|---|
| F.7 uploads not attributable | All 14,458 trail entries said `task=unspecified`; 229 remote calls had no prompt name | A `Prompt` names its task and `audit.record` takes it from there; an unregistered prompt reads `unregistered`. History is attributed when read, from each entry's recorded prompt name; the file is never rewritten | `tests/test_audit_attribution.py` (history half mutated out and caught) |
| F.12 chunk stamp | `sentence-transformers` for any model the backend loaded, so a same-dimension model would have shared bge-small's space | bge-small keeps the bare stamp (what all 168,873 rows mean, so nothing is re-embedded or migrated); any other model is stamped with its name, shortened with a hash if needed to fit `String(64)` | `tests/test_chunk_model_stamp.py` |
| F.13 stub entries | 12,520 `stub` entries from 2026-08-17 to 09-18 in the live trail | Left as they are, decision recorded: they never left the machine, quota counts today only, and the suite has had its own trail since | `packages/llm/CLAUDE.md` |

The owner's real trail, read through the new code (in memory; the file's
SHA-256 was the same before and after): **732 uploads = 429 `tailor_resume`
+ 74 `assistant` + 229 `unregistered`**. All 14,458 entries:
`tailor_resume` 8,222, `unregistered` 5,754, `assistant` 237,
`classify_inbound_email` 215, `write_cover_letter` 30.

## Not done

- The 229 `unregistered` uploads were scripts and experiments, such as
  question writers and judges for the retrieval benchmarks, using prompts of
  their own. Naming them means registering those prompts or passing a task
  through the provider, and neither is built.
- Nothing on the owner's machine was changed in this phase.

## The plan, end to end

| Phase | PR | Owner actions still open |
|---|---|---|
| 1 Submission truth, test-DB isolation | #164 (merged) | Confirm worker restart on the merged code |
| 2 Network boundary | #165 (merged as `50a7eed`) | Decide where private files live (iCloud); a non-superuser DB role |
| 3 Status truth, reconciliation | #166 (merged) | Settle or keep the 5 fences on `/finish`; choose notifications |
| 4 Audit attribution | #167 (merged) | — |

Merge state checked again on 2026-10-18: #164–#167 are merged; #165 merged
as `50a7eed`. The phase PRs were based independently on `main`. #164 also
carries the audit document itself. Current handoff and verification results
are in `docs/HARNESS_AUDIT_2026-10-14.md`.
