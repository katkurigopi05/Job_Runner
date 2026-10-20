import type { KeywordHit } from "@/lib/api";

/**
 * Why the search kept this posting: each keyword where the posting says it,
 * with two words either side, so a match on "AI" in "AI Platform" reads
 * differently from one in "we evaluate AI vendors".
 */
export function KeywordHits({ hits }: { hits: KeywordHit[] }) {
  if (hits.length === 0) return null;
  return (
    <ul className="mt-3 space-y-1" aria-label="Where your keywords matched">
      {hits.map((hit, index) => (
        <li key={`${index}-${hit.keyword}`} className="flex gap-2 text-sm text-ink-soft">
          <span className="w-20 shrink-0 pt-0.5 font-mono text-xs text-ink-faint">
            {hit.field}
          </span>
          <span className="min-w-0">
            {hit.before ? `${hit.before} ` : ""}
            <mark
              className="rounded-sm bg-attn/25 px-0.5 text-ink"
              title={`matched "${hit.keyword}"`}
            >
              {hit.match}
            </mark>
            {hit.after ? ` ${hit.after}` : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}
