import { Lock } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { percent, type StudioAts, type StudioPreview } from "@/lib/studio-api";

const TERM_GROUPS: {
  status: string;
  title: string;
  variant: "go" | "wait" | "stop";
}[] = [
  { status: "covered", title: "Already on the page", variant: "go" },
  { status: "supported synonym", title: "Said another way", variant: "wait" },
  {
    status: "supported, not stated",
    title: "Backed, not yet stated",
    variant: "wait",
  },
  {
    status: "gap",
    title: "Nothing you wrote backs these, so none is added",
    variant: "stop",
  },
];

function Score({
  label,
  before,
  after,
}: {
  label: string;
  before: number;
  after: number;
}) {
  const moved = after - before;
  const tone =
    moved > 0 ? "text-go" : moved < 0 ? "text-stop" : "text-ink-soft";
  return (
    <div>
      <dt className="font-mono text-[11px] uppercase tracking-widest text-ink-soft">
        {label}
      </dt>
      <dd className="mt-1 flex items-baseline gap-2">
        <span className="font-display text-3xl tabular-nums">
          {percent(after)}
        </span>
        <span className={`font-mono text-xs ${tone}`}>
          from {percent(before)}
        </span>
      </dd>
    </div>
  );
}

function Findings({ ats }: { ats: StudioAts }) {
  if (ats.findings.length === 0)
    return <p className="text-xs text-ink-soft">No parse findings.</p>;
  return (
    <ul className="space-y-1 text-xs text-ink-soft">
      {ats.findings.map((finding) => (
        <li key={finding}>{finding}</li>
      ))}
    </ul>
  );
}

export function Readout({ preview }: { preview: StudioPreview }) {
  const chosen = preview.projects.filter((p) => p.chosen);
  const held = preview.projects.filter((p) => !p.chosen && p.held_back);
  return (
    <section aria-labelledby="readout-heading" className="space-y-6">
      <h2 id="readout-heading" className="sr-only">
        How an ATS reads the draft
      </h2>
      <dl className="grid grid-cols-2 gap-6">
        <Score
          label="Parse"
          before={preview.ats_before.parse}
          after={preview.ats_after.parse}
        />
        <Score
          label="Posting terms"
          before={preview.ats_before.keywords}
          after={preview.ats_after.keywords}
        />
      </dl>
      <Findings ats={preview.ats_after} />

      <div className="space-y-3">
        {TERM_GROUPS.map((group) => {
          const terms = preview.terms.filter((t) => t.status === group.status);
          if (terms.length === 0) return null;
          return (
            <div key={group.status}>
              <h3 className="text-xs text-ink-soft">{group.title}</h3>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {terms.map((term) => (
                  <Badge key={term.term} variant={group.variant}>
                    {term.term}
                  </Badge>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      <div className="space-y-2">
        <h3 className="text-xs text-ink-soft">Projects printed</h3>
        {chosen.length === 0 ? (
          <p className="text-xs text-ink-faint">None.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {chosen.map((project) => (
              <li
                key={project.id}
                className="flex flex-wrap items-center gap-2"
              >
                {project.is_private ? (
                  <Lock
                    className="h-3 w-3 text-ink-soft"
                    aria-label="private"
                  />
                ) : null}
                <span>{project.name}</span>
                {project.matched_terms.length > 0 ? (
                  <span className="font-mono text-[11px] text-ink-faint">
                    {project.matched_terms.slice(0, 5).join(", ")}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
        {held.length > 0 ? (
          <ul className="space-y-0.5 text-xs text-ink-faint">
            {held.map((project) => (
              <li key={project.id}>
                {project.name}: {project.held_back}
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {preview.added.length + preview.skipped.length > 0 ? (
        <p className="text-xs text-ink-soft">
          {preview.added.length > 0
            ? `Added: ${preview.added.join("; ")}. `
            : ""}
          {preview.skipped.length > 0
            ? `Already on the résumé: ${preview.skipped.join("; ")}.`
            : ""}
        </p>
      ) : null}
    </section>
  );
}
