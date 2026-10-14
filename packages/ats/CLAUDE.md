# Notes on the ATS adapters

Moved here from the root `CLAUDE.md` on 2026-10-11, word for word, so they
load only when work touches this directory. "§N" below is a section of
the root file, which keeps every heading and says where each section went.

## §8: ATS adapter contract

`Question.kind` ∈ `text`, `textarea`, `email`, `phone`, `url`, `single_select`,
`multi_select`, `radio`, `checkbox`, `boolean`, `date`, `file`, `cover_letter`,
`typeahead`, `hidden`, `display`.

`FillReport` records every field filled, every field skipped, and the exact text of any
question that could not be answered. `Receipt` holds the field-by-field record plus a
screenshot path — this is the audit trail the owner reviews.

Adapter build order: **Greenhouse → Lever → Ashby → Workable**. Greenhouse first: no login,
stable DOM. Workday last or never — it needs per-employer accounts and is the hardest target.

## From §15: What the gates do and do not prove

### Nothing ever went to the application form

A pipeline check found `fill` and `submit` raising `NotImplementedError` on
Lever, Ashby and Workable, and live forms whose fields could not be read. The
second half turned out not to be an adapter problem at all.

`_run_pipeline` opened the URL the crawler recorded and enumerated fields
straight after `domcontentloaded`. On Greenhouse that works — the posting page
*is* the form. On the other three the form is a separate route (`/apply`,
`/application`, `/apply`), and on all three it is React that mounts after the
document is ready. So enumeration read an empty page and the adapters reported
`no application form found on page`, which reads as a broken selector and sent
the investigation to the adapters. A live probe of those routes found 40 Lever
controls and nine Ashby fields sitting there the whole time.

`packages/ats/navigate.py` holds the mechanics; the segment and the selectors
stay in each adapter, so §8 is intact. Three details are load-bearing:

- **`wait_for_form` waits for a rendered *field*, not the form element.** On
  Ashby the form selector is `body`, which is attached before any of the page
  exists, so waiting on the form alone is worthless there.
- **A captcha is checked before a timeout is reported.** Otherwise a blocked
  page fails as `site_error` — our side is broken, retry — instead of
  `manual_completion_required`.
- **`locate_form` takes the candidate with the most fields**, not the first in
  document order. Workable's live form carries no `action`; the fixture that
  passed for months did, because it was written beside the selector that reads
  it — the §15 pattern again. Admitting a bare `form` therefore also matches
  the site search box, and "the one with the questions on it" separates them
  without another site-specific rule.

**The same defect sat one step earlier, and that half was worse.** A live Ashby
posting has `h1` count 0 at `domcontentloaded` and 1 three seconds later, so
`parse_posting` returned a None title, a None location and an empty
`description_raw`. Nothing failed. The application went on to be tailored
against an empty job description, scored for ATS keywords against no
vocabulary, and filtered on a location nobody had read — and all three of those
report as *this posting is a weak match* rather than as *we never looked at
it*. `wait_for_posting` is deliberately best-effort and never raises: a
withdrawn posting has no title either, and raising would turn `job_closed` into
a retry-inviting `site_error`.

Two more read the page wrong once it did render:

- **The description was the first match, not the longest.** Lever splits a
  posting across eight `.section-wrapper .section` blocks and the first is the
  header, so a 4,967-character posting parsed to 109 characters. Not empty, so
  nothing downstream looked broken. Live after the fix: Lever 109 → 4,967,
  Ashby 0 → 4,613.
- **Ashby's location was the whole sidebar** — `Location United Kingdom;
  Germany; New York; Poland Employment Type Full time Location Type Remote
  Department Operations` — which `locality.py` then has to make a region
  decision from. Hard filters *exclude*, so a garbled location is silent. Read
  from its own section now, with `Location Type` folded in because
  `reads_as_remote` looks at this field.

### Filling it, and what the adapters already knew

`packages/ats/form.py` is the fill and submit path for all four. What differs
between the ATSes is how the form is found and what the fields are called, and
`enumerate_fields` has answered both by then — so four copies of the mechanics
would only drift. Two rules in there carry guarantees rather than convenience:

- **A control that cannot be set is parked when the question was required**,
  and only skipped when optional. Skipping a required field left
  `FillReport.is_complete` true for a form with a hole in it, and the owner was
  told an application was ready to send.
- **A dropdown answer must match an option the employer offered.** Greenhouse
  has no `<select>` elements at all — every dropdown is an `input` with
  `role="combobox"` — so `select_option` cannot touch one, and typing into a
  combobox selects nothing. §2.2 makes that the difference between a
  work-authorization answer that is recorded and one that silently is not. An
  answer nothing on the menu matches is refused, never typed in as free text.

**`profile_key_for` existed in three adapters and had no caller.** Each one
states, authoritatively, that `_systemfield_name` is the full name and
`urls[LinkedIn]` is the LinkedIn URL. `build_answers` matched a regex over the
label instead — and searched label and key *joined*, so the `^name$` rule could
never fire and Ashby's name field, labelled exactly `name`, matched nothing.
Live on an ElevenLabs posting: **1 of 9 fields filled before, 2 of 9 after**,
the remaining seven being a résumé the harness has no fixture for and six
employer questions §2.4 correctly parks.

### Half the corpus is indexed on Greenhouse and applied for somewhere else

Greenhouse's board API returns `absolute_url`, which is whatever the employer
configured. All 635 Stripe postings come back as
`stripe.com/jobs/search?gh_jid=<id>`; `detect_ats` claims none of those, and the
worker routes by URL. Of **14,892 crawled postings, 7,629 — 51% — were
unreachable by any adapter**, across databricks.com, careers.datadoghq.com,
mongodb.com, sumup.com, jobs.elastic.co and more.

The extractor now falls back to the canonical `job-boards.greenhouse.io` URL
when nothing claims the employer's own, since the slug and the id are both in
hand and no fetch is needed. **Measured honestly, that buys less than it
looks.** Stripe, Databricks and Datadog all *redirect* the canonical URL back
to their own careers page, which carries no form — 3 of 3 checked. So for those
the rewrite costs one fetch to reach the same verdict, and loses the
human-friendly link the dashboard would show.

It is kept because the verdict is now *definite and recorded* rather than a
shrug, and because an employer whose board does serve the form is a real win.
What would make it free is learning this per **company** rather than per
posting — there is no column for that yet, and it is the obvious follow-up.

**A redirect off the ATS is never a `site_error`, and it means two different
things.** The landing URL still naming the job is what separates them:

- **It does** — `job-boards.greenhouse.io/stripe/jobs/8172487` answers 200 at
  `stripe.com/careers/listing/abuse-investigator/8172487?gh_jid=8172487`. The
  employer hosts the application themselves, which `scripts/import_portals.py`
  already refuses for the same reason: we have no extractor for a bespoke page.
  `unsupported_site`.
- **It does not** — `job-boards.greenhouse.io/cloudflare/jobs/7168950` lands on
  `cloudflare.com/careers/#open-roles`, the careers index. Cloudflare's board
  is fine and we poll it successfully; this one role was taken down.
  `job_closed`, via the new `PostingGone`. Calling it `unsupported_site` would
  libel a working board, and `unsupported_site` is what decides whether a
  company stays in the registry.

This is the JSON-LD path's real motivation, restated with a number. §15's
bespoke-careers work was sized from a CSV of ~3,000 companies; half of the
boards we already poll turn out to need it too.

### What the live runs still cannot prove

`make gate-1-live` now works for any of the four ATSes, and it was run against
real boards on 2026-09-12:

| board | outcome |
|---|---|
| Cloudflare / Greenhouse | parsed 8,005 chars, then **captcha** |
| Spotify / Lever | reached `/apply`, then **captcha** |
| Palantir / Lever | reached `/apply`, then **captcha** |
| ElevenLabs / Ashby | parsed, enumerated 9 fields, filled — no captcha |
| Stripe / Greenhouse | redirected to stripe.com → `unsupported_site` |

So the fill path has now run against exactly one live employer form. §2.5 makes
the captchas the correct stopping point rather than a gap, which means **the
submit path cannot be proven live at all** — and should not be. Gate 1's live
half stays red forever by design.

Nothing was submitted. No employer form was sent.

---
