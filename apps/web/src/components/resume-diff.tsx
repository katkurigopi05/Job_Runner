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
