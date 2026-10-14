# Notes on the inbox

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

## From §15: What the gates do and do not prove

### The alias was never applied with, so no reply could conclude anything

Both halves of Phase 6's routing were built and nothing joined them.

`packages/inbox/alias.py` calls plus-addressing "the whole mechanism", and
`route.py` enforces it: an inferred link attaches a message and stops there,
and **only an exact alias may set an outcome or hand an OTP to the state
machine**. That rule is right — recording a rejection on the wrong application
is silent and wrong. But `alias_for` had no caller outside the test suite,
`Settings.inbox_alias_base` had no reader anywhere in the tree, and
`packages/ats/answers.py` typed `candidate.email` into every form. Measured
before the fix:

```text
email typed into the form : owner@gmail.com
alias the reply must carry: owner+app1b7b...@gmail.com
find_alias(typed)         : None
```

So the tag never reached an employer, no reply could carry one back, and
**nothing a recruiter sent could move an application** — not an outcome, and
not the OTP that resumes a `needs_otp` run. Every reply took the
`inbound_unrouted` branch or attached without concluding.

Gate 6 passed throughout. `email_for` in `tests/test_inbox.py` builds the
address with `alias_for` itself, so the fixture did the job the product did
not — the §15 pattern exactly, one layer deeper than the fixtures that section
already records. §15 said Gate 6 had never seen a real email; it could not have
routed one if it had.

`apply_job._reply_to` is the join, and three things about it are deliberate:

- **Managed mode only.** `email_mode` defaults to `self` and the schema already
  had the switch; nothing changes for a candidate who never opted in. This
  decides the address an employer replies to, and a `+` tag some ATS field
  rejects would cost the application itself — that risk belongs to an owner who
  asked for routing, not to everyone.
- **One key per application, not per candidate.** `managed_alias` is a mailbox,
  not an alias. Three roles at one company are three applications, and the
  point of the tag is telling their replies apart.
- **A missing mailbox applies as the candidate and says so by name.** Failing an
  application over a configuration gap costs more than the routing does, but
  reverting silently is how this stayed invisible for a whole phase, so
  `applying_with_the_candidate_address_no_usable_alias` is logged. An unusable
  base is refused outright rather than producing an alias that never routes
  back.

`alias_for` guarded only two of the shapes that produce one. It tested
`"@" not in base_address`, which a *doubled* `@` satisfies, so
`owner@@example.com` yielded `owner+app...@@example.com` — an address no
employer's form accepts, and one `parse_alias` read back as a valid alias of
ours, because its domain group was `.+`. Malformed at the employer and
correct-looking at both of our own ends, which is the combination that hides.
Empty local parts and domains passed the same way. The check is now exactly one
`@` with both sides non-empty, and the regex will not match a second `@` — the
two ends have to agree about what we could have issued, or the trail says a
reply routed when it could not have.

`tests/test_apply_reply_alias.py` closes the loop rather than testing the
halves: it applies with the address the pipeline actually produces, replies to
it, and asserts the outcome lands. Deleting the one line in `_run_pipeline`
turns three of its tests red — including the source-level assertion, which is
there because every other test in the file would still pass without it.

### A resent OTP was dropped as a duplicate

§10 says every queue handler is idempotent, and `route_message`'s docstring
said how: "Idempotent on `message_id`: IMAP re-delivers, and a rejection
recorded twice must not look like two rejections."

There was no `message_id` column. The de-duplication keyed on
`(from_addr, application_id, subject)`, which is not an identity — it is a
heuristic, and it collides on exactly the mail that legitimately repeats.

The sharp case is an OTP resend, because the duplicate check returns *before*
the block that hands a code to the state machine:

```text
first  111111 -> status='running'   changed=True   otp='111111'
resend 222222 -> status='needs_otp' changed=False  otp='111111'
```

The owner clicks "resend code" — which is what you do when the first one
expired — and the second never arrives. The application sits in `needs_otp`
holding the dead code, and §6's `needs_otp ──otp──> running` edge is
unreachable for the rest of that run. A follow-up on an existing thread is the
same collision without the state machine attached: same sender, same subject,
silently swallowed.

**Fixing it needed `imap.py` fixed first.** The stand-in for a message with no
`Message-ID` header was `f"no-id-{id(raw)}"` — a *memory address*. Unstable
between runs, so a re-delivered message would be recorded twice; reusable
after collection, so a later, different message could inherit a freed address
and be dropped. Both directions were harmless only while nothing keyed on it,
and making the id the key would have inherited both. It is a digest of the
bytes now: the identity the header would have carried.

The column is nullable and **deliberately not backfilled**. Existing rows have
no id to recover, and inventing one would make two different messages look
like the same message — the failure the column exists to prevent. A NULL never
matches, so old rows cannot suppress new mail.

**The check and the insert were still two statements**, which review caught and
which the column alone did not fix. `run_pool` runs several workers, and
`search(None, "UNSEEN")` hands the same ids to every `handle_inbox` that asks
before one of them FETCHes and marks them seen. Two routings of one message
could both pass a SELECT — neither transaction can see the other's uncommitted
row — and both would insert and both would apply the outcome or the OTP.

`uq_inbound_messages_candidate_message` is a *unique* partial index, and
`route_message` claims the message with `INSERT ... ON CONFLICT DO NOTHING`
rather than reading first. Losing the race is a no-op instead of a second row,
and the invariant is true by construction rather than by timing. The path for
a message with no id keeps the old triple and with it the old race: there is no
key a unique index could be built on, and it is reachable only from a caller
constructing an `InboundEmail` by hand.

`tests/test_inbox_duplicates.py` is in `GATE6_TESTS`, and covers both
directions: a resend reaches the state machine, and a genuine re-delivery is
still recorded once. Reverting either half turns four of them red.

The race test took two tries to be one. It cannot use the `db_session`
fixture — that runs inside a transaction which is rolled back, so nothing it
writes reaches a second connection and both routings simply find no
application. And committing both sessions at the end *deadlocks*: the second
insert waits on the first's uncommitted row, so the first has to commit while
the second is still blocked. The first draft hung rather than failed, which is
its own kind of test that proves nothing.

One of its tests had to be repaired before it was worth having.
`_synthetic_id(raw) == _synthetic_id(bytes(raw))` passes against the *old*
implementation too, because `bytes()` on a bytes input returns the same object
in CPython, so the two addresses matched. Round-tripping through a bytearray
forces a separate allocation. A test that cannot fail is the thing this file
keeps finding.
