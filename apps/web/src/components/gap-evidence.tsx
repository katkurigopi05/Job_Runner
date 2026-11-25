"use client";

import { useState } from "react";
import { APPLY_LINK } from "@/lib/external";
import {
  citationsOf,
  DECLARATION_NOTE,
  declarationOf,
  GAP_PAGE,
  nextIds,
  pinOf,
  shownOf,
  stateOf,
  type GapEvidence,
  type GapJob,
} from "@/lib/gap-evidence";

const LINK =
  "text-ink-soft underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft";

const MORE_BUTTON =
  "rounded-lg border border-rule px-3 py-1 text-xs text-ink-soft transition-colors hover:border-ink-soft hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft disabled:opacity-60";

/** The jobs and projects behind each recurring gap the answer lists. */
export function GapEvidenceList({ gaps }: { gaps: GapEvidence[] }) {
  return (
    <div className="mt-2 space-y-4">
      {gaps.map((gap) => (
        <GapDrilldown key={gap.skill} gap={gap} />
      ))}
      <p>{DECLARATION_NOTE}</p>
    </div>
  );
}

async function readJobs(gap: GapEvidence, ids: string[]): Promise<GapJob[]> {
  const response = await fetch("/api/chat/gap-jobs", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ skill: gap.skill, posting_ids: ids }),
  });
  if (!response.ok) throw new Error(`The jobs could not be read (${response.status}).`);
  const body = (await response.json()) as { jobs?: GapJob[] };
  return body.jobs ?? [];
}

function GapDrilldown({ gap }: { gap: GapEvidence }) {
  const [jobs, setJobs] = useState<GapJob[]>(gap.jobs);
  // Ids asked for so far: a job deleted since the answer is not returned,
  // and must not be asked for again.
  const [asked, setAsked] = useState(gap.jobs.length);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const remaining = gap.total - asked;

  async function more(all: boolean) {
    setBusy(true);
    setError(null);
    let read = asked;
    try {
      do {
        const ids = nextIds(gap, read);
        if (ids.length === 0) break;
        const found = await readJobs(gap, ids);
        read += ids.length;
        setJobs((current) => [...current, ...found]);
        setAsked(read);
      } while (all && read < gap.total);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "The jobs could not be read.");
    } finally {
      setBusy(false);
    }
  }

  const gone = asked - jobs.length;
  return (
    <section aria-label={`${gap.label}: the jobs behind the count`} className="space-y-1.5">
      <p className="text-ink">
        <strong>{gap.label}</strong> [{gap.source_label}] · directly required by{" "}
        {gap.total.toLocaleString("en-US")} of {gap.reviewed.toLocaleString("en-US")} jobs reviewed
      </p>
      <p>{shownOf(jobs.length, gap.total, gone)}</p>
      <ol className="space-y-1 border-l border-rule-soft pl-2.5">
        {jobs.map((job) => {
          const changed = stateOf(job);
          return (
            <li key={job.posting_id}>
              <a href={job.url} className={LINK} {...APPLY_LINK}>
                {job.title}
              </a>
              {job.company ? ` — ${job.company}` : ""}
              {job.location ? ` · ${job.location}` : ""}
              {changed ? ` · ${changed}` : ""}
              {job.quote ? <q className="block pl-2 italic">{job.quote}</q> : null}
            </li>
          );
        })}
      </ol>
      {remaining > 0 ? (
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" disabled={busy} onClick={() => more(false)} className={MORE_BUTTON}>
            Show next {Math.min(GAP_PAGE, remaining).toLocaleString("en-US")} ·{" "}
            {remaining.toLocaleString("en-US")} more
          </button>
          {remaining > GAP_PAGE ? (
            <button type="button" disabled={busy} onClick={() => more(true)} className={MORE_BUTTON}>
              Show all {remaining.toLocaleString("en-US")}
            </button>
          ) : null}
        </div>
      ) : null}
      {error ? (
        <p role="status" className="text-attn">
          {error}
        </p>
      ) : null}
      {gap.projects.length > 0 ? (
        <>
          <p className="text-ink">Projects whose repositories declare or implement {gap.label}:</p>
          <ul className="space-y-0.5 border-l border-rule-soft pl-2.5">
            {gap.projects.map((project) => {
              const cited = citationsOf(project);
              return (
                <li key={project.project_id}>
                  <a href={project.url} className={LINK} {...APPLY_LINK}>
                    {project.repository}
                  </a>
                  {project.declared_in.length > 0 ? ` · ${declarationOf(project)}` : ""}
                  {` · ${pinOf(project)}`}
                  {cited.length > 0 ? (
                    <ul className="mt-0.5 space-y-0.5 pl-3">
                      {cited.map((cite) => (
                        <li key={cite.url}>
                          Implemented in{" "}
                          <a href={cite.url} className={LINK} {...APPLY_LINK}>
                            {cite.label}
                          </a>
                          {` ${cite.pin} (checked by a Resume Studio save)`}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </>
      ) : (
        <p>No eligible project declares {gap.label}.</p>
      )}
    </section>
  );
}
