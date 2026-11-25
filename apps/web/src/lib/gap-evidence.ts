/**
 * A recurring skill gap's drill-down: the jobs behind "required by N jobs", and
 * the projects whose repositories declare the skill, or implement it in a file
 * a Resume Studio save verified at a commit.
 *
 * As the API sends it (`ChatReply.graph_gaps`, `packages/matching/gap_evidence.py`).
 * The answer carries the id of every one of the N, fixed when it was written,
 * and the first few jobs in full; the rest are read on request by those ids
 * (`POST /api/chat/gap-jobs`), so the list is always the population the count
 * was taken over.
 *
 * Pure, so Node can run it (`tests/gap-evidence.test.mjs`).
 */

export interface GapJob {
  posting_id: string;
  title: string;
  company: string | null;
  location: string | null;
  url: string;
  /** The job's own line requiring the skill. Null only when read again and gone. */
  quote: string | null;
  state: "as_answered" | "current" | "closed" | "changed";
}

/** A file and lines that implement the skill, at a commit a Studio save checked. */
export interface CodeCitation {
  path: string;
  start_line: number;
  end_line: number;
  commit_sha: string;
  /** The lines on github.com at that commit. */
  url: string;
  explanation: string;
}

export interface GapProject {
  project_id: string;
  repository: string;
  url: string;
  /** "declared", or "implemented" only with `implemented_in` citations. */
  category: string;
  declared_in: Array<"dependency_file" | "primary_language">;
  /** The commit a live GitHub check read it at; null for imported metadata. */
  commit_sha: string | null;
  /** Absent on answers written before Studio findings were kept. */
  implemented_in?: CodeCitation[];
}

export interface GapEvidence {
  skill: string;
  label: string;
  /** The source the answer cites for this gap, e.g. "P1". */
  source_label: string;
  /** N: distinct jobs requiring it among the `reviewed`. */
  total: number;
  reviewed: number;
  posting_ids: string[];
  jobs: GapJob[];
  projects: GapProject[];
}

/** Jobs read per click. The API reads at most 50 at a time. */
export const GAP_PAGE = 25;

/**
 * Repository evidence is not the owner's own work. Said once under the gaps,
 * in the words Resume Studio asks the question in.
 */
export const DECLARATION_NOTE =
  "A declaration means the repository lists the technology, not that its code uses it. " +
  "A cited file is code a Resume Studio save verified at that commit. Neither means the " +
  "work was yours: confirm your contribution in Resume Studio before claiming it.";

function isGap(value: unknown): value is GapEvidence {
  if (typeof value !== "object" || value === null) return false;
  const gap = value as Partial<GapEvidence>;
  return (
    typeof gap.skill === "string" &&
    typeof gap.label === "string" &&
    typeof gap.total === "number" &&
    typeof gap.reviewed === "number" &&
    Array.isArray(gap.posting_ids) &&
    Array.isArray(gap.jobs) &&
    Array.isArray(gap.projects)
  );
}

/** A reply's or a saved turn's gaps. Answers from before drill-downs have none. */
export function gapsOf(value: unknown): GapEvidence[] {
  return Array.isArray(value) ? value.filter(isGap) : [];
}

/**
 * How much of the N is on screen. `gone` are ids read again and no longer
 * found (deleted since the answer): not hidden, so not "of N" still to come.
 */
export function shownOf(shown: number, total: number, gone: number = 0): string {
  const all = total.toLocaleString("en-US");
  const missing = gone > 0 ? ` · ${gone.toLocaleString("en-US")} no longer exist` : "";
  if (shown + gone >= total) {
    return gone > 0 ? `All ${all} jobs read${missing}` : `All ${all} ${total === 1 ? "job" : "jobs"} shown`;
  }
  return `Showing ${shown.toLocaleString("en-US")} of ${all} jobs${missing}`;
}

/** The next ids to read: the ones after those shown, at most `page`. */
export function nextIds(gap: GapEvidence, shown: number, page: number = GAP_PAGE): string[] {
  return gap.posting_ids.slice(shown, shown + page);
}

/** A declaration, said as one: never "implemented", whatever else is known. */
export function declarationOf(project: GapProject): string {
  const where = project.declared_in.map((place) =>
    place === "dependency_file" ? "in a dependency file" : "as its primary language",
  );
  return `Declared ${where.join(" and ")}`;
}

/** A cited file, said as where it is: never whose work it was. */
export interface CitationView {
  label: string;
  pin: string;
  url: string;
}

/** The files a Studio save verified for this project and skill; none for a declaration. */
export function citationsOf(project: GapProject): CitationView[] {
  return (project.implemented_in ?? []).map((cite) => ({
    label:
      cite.start_line === cite.end_line
        ? `${cite.path} line ${cite.start_line}`
        : `${cite.path} lines ${cite.start_line}–${cite.end_line}`,
    pin: `at commit ${cite.commit_sha.slice(0, 10)}`,
    url: cite.url,
  }));
}

/** Which version of the repository the declaration was read from. */
export function pinOf(project: GapProject): string {
  return project.commit_sha
    ? `checked live at commit ${project.commit_sha.slice(0, 10)}`
    : "from imported metadata, not pinned to a commit";
}

/** What changed about a job read again since the answer, if anything. */
export function stateOf(job: GapJob): string {
  if (job.state === "closed") return "closed since this answer";
  if (job.state === "changed") return "no longer states this requirement";
  return "";
}
