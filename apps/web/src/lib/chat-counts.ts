/** Labels for exact totals computed by retrieval, never counted by the model. */
interface MatchCounts {
  matchedTotal?: number | null;
  matchedRole?: string | null;
  titleWords?: string[];
  relatedTotal?: number;
  matchedTerm?: string | null;
  matchedCompanies?: string[];
  companiesTotal?: number | null;
}

export function countedMatches(turn: MatchCounts): string | null {
  const total = turn.matchedTotal ?? null;
  if (total === null) return null;
  const plural = total === 1 ? "" : "s";
  if (turn.matchedRole) {
    return `${total} open ${turn.matchedRole} posting${plural} in your search area`;
  }
  if (turn.titleWords?.length) {
    const related = turn.relatedTotal ? `, then ${turn.relatedTotal} related without it` : "";
    return `${total} open posting${plural} with ${turn.titleWords.join(" and ")} in the title, in your search area${related}`;
  }
  if (turn.matchedTerm) {
    return `${total} open posting${plural} at ${turn.companiesTotal ?? 0} companies mentioning ${turn.matchedTerm} in your search area; mentions do not necessarily mean required skills`;
  }
  if (turn.matchedCompanies?.length) {
    return `${total} open posting${plural} at ${turn.matchedCompanies.join(" or ")} in your search area`;
  }
  return null;
}
