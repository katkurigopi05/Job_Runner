/** Retrieval scope and optional source-derived diagnostics, outside answer prose. */
export function AssistantEvidence({ searched = 0, unsearchable = 0, scope, details, method }: {
  searched?: number;
  unsearchable?: number;
  scope?: "open_postings" | "top_matches";
  details?: string | null;
  method?: "standard" | "graph";
}) {
  return (
    <>
      <p className="text-ink-faint">
        {method === "graph" ? "GraphRAG · " : ""}
        {scope === "top_matches"
          ? `Based on ${searched.toLocaleString("en-US")} of your top job matches`
          : `From a search of ${searched.toLocaleString("en-US")} of ${(searched + unsearchable).toLocaleString("en-US")} open postings`}
      </p>
      {details ? (
        <details className="text-ink-faint">
          <summary className="cursor-pointer">Evidence details</summary>
          <p className="mt-2 whitespace-pre-wrap break-words">{details}</p>
        </details>
      ) : null}
    </>
  );
}
