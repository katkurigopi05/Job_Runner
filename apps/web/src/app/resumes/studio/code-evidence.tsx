"use client";

import { useState } from "react";
import type { StudioCodeApproval, StudioCodeReview } from "@/lib/studio-api";
import { CITATION_LINK } from "@/lib/external";

export function CodeEvidenceReview({
  review,
  approvals,
  onChange,
}: {
  review: StudioCodeReview;
  approvals: StudioCodeApproval[];
  onChange: (approvals: StudioCodeApproval[]) => void;
}) {
  const [wording, setWording] = useState<Record<string, string>>({});
  const findings = new Map(review.findings.map((finding) => [finding.id, finding]));
  return (
    <section aria-labelledby="code-evidence-heading" className="space-y-4">
      <h2 id="code-evidence-heading" className="font-display text-xl">Live GitHub code evidence</h2>
      {review.limitations.map((notice) => (
        <p key={notice} className="text-sm text-attn" role="status">{notice}</p>
      ))}
      {review.requirements.length === 0 ? (
        <p className="text-sm text-ink-soft">No recognized technology requirements in this posting.</p>
      ) : null}
      <ul className="divide-y divide-rule">
        {review.requirements.map((requirement) => (
          <li key={requirement.skill} className="space-y-1 py-2 text-sm">
            <p>
              <strong>{requirement.label}</strong> · {requirement.demand}
              {requirement.option ? " alternative" : ""} · {requirement.status}
            </p>
            <p className="text-xs text-ink-soft">{requirement.quote}</p>
            {requirement.resume_sources.length > 0 ? (
              <p className="text-xs text-ink-soft">
                Résumé sources ({requirement.resume_sources.join(", ")})
              </p>
            ) : null}
            {requirement.finding_ids.length > 0 ? (
              <ul className="space-y-1">
                {requirement.finding_ids.map((id) => {
                  const finding = findings.get(id);
                  if (!finding) return null;
                  const location = `${finding.full_name} · ${finding.path}:${finding.start_line}–${finding.end_line}`;
                  return (
                    <li key={id} className="text-xs text-ink-soft">
                      {finding.permalink ? (
                        <a className="underline" href={finding.permalink} {...CITATION_LINK}>
                          {location}
                        </a>
                      ) : <span>{location}</span>}
                      {" · "}{finding.classification} · {finding.verification_status}
                      {" · "}<code title={finding.commit_sha}>{finding.commit_sha.slice(0, 10)}</code>
                      <p>{finding.explanation}</p>
                    </li>
                  );
                })}
              </ul>
            ) : null}
          </li>
        ))}
      </ul>
      {review.context.length > 0 ? (
        <details className="text-sm text-ink-soft">
          <summary>Responsibilities and qualifications from the posting</summary>
          <ul className="mt-2 space-y-1 text-xs">
            {review.context.map((item, index) => (
              <li key={`${item.kind}-${index}`}>
                {item.kind}{item.demand ? ` · ${item.demand}` : ""} — {item.quote}
                {item.related_skills.length ? ` (${item.related_skills.join(", ")})` : ""}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {review.suggestions.length > 0 ? (
        <fieldset className="space-y-3 border-t border-rule pt-3">
          <legend className="font-display text-lg">Proposed Projects claims</legend>
          <p className="text-xs text-ink-soft">
            Confirm your own contribution to include a claim when saving the PDF.
            Leave a claim unchecked to skip it.
          </p>
          {review.suggestions.map(({ finding_id, project_name, entry, wording: choices }) => {
            const approved = approvals.find((item) => item.finding_id === finding_id);
            const selected = approved?.wording ?? wording[finding_id] ?? choices[0];
            return (
              <div key={finding_id} className="space-y-1 text-sm">
                <label className="grid gap-1">
                  <span>{project_name} · {findings.get(finding_id)?.full_name}</span>
                  <select
                    className="w-full rounded border border-rule bg-paper p-1.5"
                    value={selected}
                    onChange={(event) => {
                      const text = event.target.value;
                      setWording((current) => ({ ...current, [finding_id]: text }));
                      if (approved) onChange(approvals.filter((item) => item.finding_id !== finding_id));
                    }}
                  >
                    {choices.map((choice) => <option key={choice}>{choice}</option>)}
                  </select>
                </label>
                <label className="flex gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={Boolean(approved)}
                    disabled={!approved && approvals.length >= 12}
                    onChange={(event) => onChange(event.target.checked
                      ? [...approvals, { finding_id, entry, wording: selected, contribution_confirmed: true }]
                      : approvals.filter((item) => item.finding_id !== finding_id))}
                  />
                  I confirm I did this work and approve this wording.
                </label>
              </div>
            );
          })}
        </fieldset>
      ) : null}
    </section>
  );
}
