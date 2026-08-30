import type { ResumeDiff } from "@/lib/api";

/**
 * Which model wrote the document being approved.
 *
 * §7 routes tailoring to the best available provider and, when the daily
 * remote allowance is spent or that provider is unreachable, falls back to the
 * local model rather than refusing. A résumé written by llama3.1 after the
 * allowance ran out is a different document from one written by Gemini, and
 * the moment that difference matters is this screen — where the owner decides
 * whether to send it.
 *
 * Renders "not recorded" rather than nothing when the field is absent. Every
 * résumé tailored before the column existed is in that state, and a silently
 * missing line reads as "no fallback happened", which is the one thing it must
 * not be mistaken for.
 */
function TailoredBy({ model }: { model?: string | null }) {
  return (
    <p className="font-mono text-xs text-ink-faint">
      written by{" "}
      {model ? (
        <span className="text-ink-soft">{model}</span>
      ) : (
        <span className="italic">not recorded</span>
      )}
    </p>
  );
}

function Score({ label, before, after }: { label: string; before: number; after: number }) {
  const delta = after - before;
  const tone = delta < -0.001 ? "text-stop" : delta > 0.001 ? "text-go" : "text-ink-faint";
  return (
    <div className="flex items-baseline gap-2">
      <span className="w-20 font-mono text-xs text-ink-faint">{label}</span>
      <span className="font-mono text-sm text-ink-soft">{Math.round(before * 100)}%</span>
      <span className="text-ink-faint">→</span>
      <span className="font-mono text-sm">{Math.round(after * 100)}%</span>
      {Math.abs(delta) > 0.001 ? (
        <span className={`font-mono text-xs ${tone}`}>
          {delta > 0 ? "+" : ""}
          {Math.round(delta * 100)}
        </span>
      ) : null}
    </div>
  );
}

/**
 * How an ATS reads this résumé, before and after tailoring, against this posting.
 *
 * Both halves are shown and never averaged. They fail independently: a run that
 * raises keyword coverage while lowering the parse score has made the document
 * worse in the way that matters most, because a résumé an ATS cannot segment is
 * a row of empty columns and no amount of keyword matching rescues it.
 *
 * `gained` is the part worth reading. Coverage moving from 31% to 37% says
 * nothing about whether the terms it picked up were worth having; the list of
 * terms does. None of them is invented — tailoring can only surface vocabulary
 * the source résumé already supported, which is what the guard holds.
 */
function AtsPanel({ ats }: { ats: NonNullable<ResumeDiff["ats"]> }) {
  return (
    <div className="space-y-2 border border-rule bg-paper px-3 py-2.5">
      <p className="font-mono text-xs uppercase tracking-wide text-ink-faint">
        How an ATS reads this
      </p>
      <Score label="parse" before={ats.parse_before} after={ats.parse_after} />
      <Score label="keywords" before={ats.keywords_before} after={ats.keywords_after} />

      {ats.parse_after < ats.parse_before ? (
        <p className="text-sm text-stop">
          Tailoring made this document harder for a parser to read. That costs more than any
          keyword it gained — check the résumé below before approving.
        </p>
      ) : null}

      {ats.gained.length > 0 ? (
        <p className="text-sm text-ink-soft">
          <span className="text-go">Now matching:</span> {ats.gained.join(", ")}
        </p>
      ) : null}

      {ats.still_missing.length > 0 ? (
        <p className="text-sm text-ink-faint">
          <span className="text-ink-soft">Still unmatched:</span> {ats.still_missing.join(", ")}.
          These are terms the posting asks for that your résumé does not back. Tailoring cannot
          add them — if one is genuinely true and simply unwritten, edit your résumé below.
        </p>
      ) : null}
    </div>
  );
}

/**
 * What a person is likely to make of it.
 *
 * Beside the ATS panel and never merged with it. The two answer different
 * questions and can disagree, and the disagreement is the useful part: a
 * rewrite that packs the posting's words into every bullet raises keyword
 * coverage and lowers this. Averaging them would hide exactly the trade the
 * owner is here to judge.
 *
 * Credibility leads the levels because it is the one that ends candidacies and
 * the one tailoring cannot repair — a Skills list naming technologies the
 * experience never shows reads as inflation to a person, whatever it scores on
 * a keyword match.
 */
function RecruiterPanel({ recruiter }: { recruiter: NonNullable<ResumeDiff["recruiter"]> }) {
  const regressed = recruiter.after < recruiter.before;
  return (
    <div className="space-y-2 border border-rule bg-paper px-3 py-2.5">
      <p className="font-mono text-xs uppercase tracking-wide text-ink-faint">
        How a person reads this
      </p>
      <Score label="overall" before={recruiter.before} after={recruiter.after} />
      <p className="font-mono text-xs text-ink-soft">
        shortlist: {recruiter.shortlist_before} → {recruiter.shortlist_after}
      </p>

      {regressed ? (
        <p className="text-sm text-stop">
          Tailoring made this read worse to a human than the original did. If the ATS score went
          up at the same time, the rewrite bought keywords at the cost of the reader — check the
          document below before approving.
        </p>
      ) : null}

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-xs text-ink-soft">
        <div className="flex justify-between">
          <dt>credibility</dt>
          <dd>{Math.round(recruiter.credibility_after * 100)}%</dd>
        </div>
        <div className="flex justify-between">
          <dt>10-second scan</dt>
          <dd>{Math.round(recruiter.scan_after * 100)}%</dd>
        </div>
        <div className="flex justify-between">
          <dt>qualification</dt>
          <dd>{Math.round(recruiter.qualification_after * 100)}%</dd>
        </div>
        <div className="flex justify-between">
          <dt>technical</dt>
          <dd>{Math.round(recruiter.technical_after * 100)}%</dd>
        </div>
      </dl>

      {recruiter.findings.length > 0 ? (
        <ul className="space-y-1 text-sm text-ink-soft">
          {recruiter.findings.map((finding, index) => (
            <li key={index}>{finding}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/**
 * What tailoring changed, before it is sent.
 *
 * CLAUDE.md §2.1 says a rewrite may rephrase and re-emphasize but may never
 * introduce a fact the source résumé does not support. The guard enforces
 * that; this screen is where the owner gets to check it. A guard whose
 * decisions are never shown is indistinguishable from a guard that never ran.
 *
 * `rejected` is the number the guard refused and replaced with the original
 * line. It is reported rather than hidden — a high count means the model kept
 * trying to invent, which is worth knowing before approving.
 */
export function ResumeDiffView({ diff }: { diff: ResumeDiff }) {
  // The reuse paths carry no counts at all: an overnight batch or a cache hit
  // attached a document written in an earlier run, and there is nothing about
  // *this* run to diff. Checked before the counts because `changed` is
  // undefined here, and `undefined === 0` is false — which used to fall
  // through to the branch below and map over an undefined `changes`.
  // Checked before `reused`, which the pinned paths also set. The owner having
  // chosen this document is the more specific fact and the one with
  // consequences: everything below describes the résumé theirs was derived
  // from, not the file that will be uploaded.
  if (diff.owner_pinned) {
    const byHand = diff.owner_pinned === "owner_edit";
    return (
      <div className="space-y-3">
        <div className="rounded-[var(--radius)] border border-attn/40 bg-attn-soft px-3 py-2">
          <p className="font-mono text-xs text-attn">
            {byHand
              ? "you edited this résumé after tailoring wrote it"
              : "you chose this version from the model comparison"}
          </p>
          <p className="mt-1.5 text-sm text-ink-soft">
            The changes below are what tailoring did to the résumé yours came from. They do not
            describe the file that will be uploaded — that one is further down, under “Résumé to
            be sent”. Approving sends your version; nothing re-tailors over it.
          </p>
        </div>
        <TailoredBy model={diff.answered_by} />
      </div>
    );
  }

  if (diff.reused) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-ink-soft">
          Reused a résumé tailored for this posting earlier. Nothing was rewritten during this
          run, and no résumé text was sent to a provider.
        </p>
        <TailoredBy model={diff.answered_by} />
      </div>
    );
  }

  const changes = diff.changes ?? [];
  const changed = diff.changed ?? 0;
  const unchanged = diff.unchanged ?? 0;
  const rejected = diff.rejected ?? 0;

  if (changed === 0) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-ink-soft">
          Tailoring changed nothing. Your résumé goes as written.
          {rejected > 0 ? (
            <>
              {" "}
              <span className="text-attn">
                {rejected} rewrite{rejected === 1 ? "" : "s"} were refused by the fabrication
                guard.
              </span>
            </>
          ) : null}
        </p>
        <TailoredBy model={diff.answered_by} />
        {/* Shown even here. "Tailoring changed nothing" and "your résumé
            matches a fifth of what this posting asks for" are different facts,
            and the second is the one that decides whether to apply at all. */}
        {diff.ats ? <AtsPanel ats={diff.ats} /> : null}
        {diff.recruiter ? <RecruiterPanel recruiter={diff.recruiter} /> : null}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <p className="font-mono text-xs text-ink-soft">
          {changed} line{changed === 1 ? "" : "s"} rewritten · {unchanged} unchanged
          {rejected > 0 ? <span className="text-attn"> · {rejected} refused by the guard</span> : null}
        </p>
        <TailoredBy model={diff.answered_by} />
      </div>

      {diff.ats ? <AtsPanel ats={diff.ats} /> : null}
      {diff.recruiter ? <RecruiterPanel recruiter={diff.recruiter} /> : null}

      <ul className="space-y-4">
        {changes.map((change, index) => (
          <li key={index} className="border border-rule bg-paper">
            <p className="border-b border-rule px-3 py-2 text-sm text-ink-faint line-through decoration-stop/50">
              {change.original}
            </p>
            <p className="px-3 py-2 text-sm">{change.tailored}</p>
          </li>
        ))}
      </ul>

      <p className="font-mono text-xs text-ink-faint">
        Nothing here may introduce a fact your source résumé does not already support. Anything
        that tried was rejected and the original kept.
      </p>
    </div>
  );
}
