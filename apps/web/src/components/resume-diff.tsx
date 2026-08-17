import type { ResumeDiff } from "@/lib/api";

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
  if (diff.changed === 0) {
    return (
      <p className="text-sm text-ink-soft">
        Tailoring changed nothing. Your résumé goes as written.
        {diff.rejected > 0 ? (
          <>
            {" "}
            <span className="text-attn">
              {diff.rejected} rewrite{diff.rejected === 1 ? "" : "s"} were refused by the
              fabrication guard.
            </span>
          </>
        ) : null}
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <p className="font-mono text-xs text-ink-soft">
        {diff.changed} line{diff.changed === 1 ? "" : "s"} rewritten · {diff.unchanged} unchanged
        {diff.rejected > 0 ? (
          <span className="text-attn"> · {diff.rejected} refused by the guard</span>
        ) : null}
      </p>

      <ul className="space-y-4">
        {diff.changes.map((change, index) => (
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
