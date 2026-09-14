import type { Compensation, EducationRequirement, PostingRequirements, SkillEvidence } from "@/lib/api";

/**
 * Pay, skills and education as the posting states them — and, as plainly,
 * what it does not state.
 *
 * Every value carries the line it was read from, one click away, because a
 * filter just excluded or kept this job on the strength of it. An unstated
 * value is written out as unstated rather than left blank: a blank pay line
 * reads as "no information", and "not stated" is information.
 */

const EDUCATION: Record<EducationRequirement["level"], string> = {
  high_school: "high-school diploma",
  associate: "associate degree",
  bachelor: "bachelor's degree",
  master: "master's degree",
  phd: "PhD",
};

function money(value: number | null, currency: string | null): string {
  if (value === null) return "?";
  try {
    return new Intl.NumberFormat("en-US", {
      style: currency ? "currency" : "decimal",
      currency: currency ?? undefined,
      maximumFractionDigits: value % 1 === 0 ? 0 : 2,
    }).format(value);
  } catch {
    return `${value.toLocaleString("en-US")} ${currency ?? ""}`.trim();
  }
}

function Evidence({ quote }: { quote: string | null }) {
  if (!quote) return null;
  return (
    <details className="group inline">
      <summary className="inline cursor-pointer list-none font-mono text-[11px] text-ink-faint underline decoration-dotted underline-offset-2 hover:text-ink">
        source
      </summary>
      <blockquote className="mt-1 border-l-2 border-rule pl-3 text-xs italic leading-relaxed text-ink-soft">
        “{quote}”
      </blockquote>
    </details>
  );
}

function PayLine({ pay }: { pay: Compensation | null }) {
  if (!pay) {
    return <p className="text-sm text-ink-faint">Pay not stated</p>;
  }
  const range =
    pay.minimum === pay.maximum
      ? money(pay.minimum, pay.currency)
      : `${money(pay.minimum, pay.currency)} – ${money(pay.maximum, pay.currency)}`;
  return (
    <p className="text-sm text-ink">
      <span className="tabular-nums">{range}</span>
      {pay.currency && !["USD", "GBP", "EUR", "INR"].includes(pay.currency) ? ` ${pay.currency}` : ""}
      <span className="text-ink-soft">
        {pay.period ? ` per ${pay.period}` : " — period not stated"}
      </span>{" "}
      <Evidence quote={pay.quote} />
    </p>
  );
}

function Chips({ label, tone, skills }: { label: string; tone: string; skills: SkillEvidence[] }) {
  if (skills.length === 0) return null;
  return (
    <div className="flex flex-wrap items-baseline gap-1.5">
      <span className={`mr-1 font-mono text-[11px] uppercase tracking-widest ${tone}`}>{label}</span>
      {skills.map((skill) => (
        <span
          key={skill.skill}
          title={skill.quote}
          className="rounded-full border border-rule-soft px-2 py-0.5 text-xs text-ink-soft"
        >
          {skill.label}
        </span>
      ))}
    </div>
  );
}

function EducationLine({ education }: { education: EducationRequirement | null }) {
  if (!education) return <p className="text-sm text-ink-faint">Education not stated</p>;
  const what = EDUCATION[education.level];
  const verb =
    education.requirement === "required"
      ? "Requires"
      : education.requirement === "preferred"
        ? "Prefers"
        : "Mentions";
  return (
    <p className="text-sm text-ink-soft">
      {verb} a {what}
      {education.equivalent_experience ? " or equivalent experience" : ""}
      {education.requirement === "unclassified" ? " (not stated whether required)" : ""}{" "}
      <Evidence quote={education.quote} />
    </p>
  );
}

export function RequirementsPanel({
  compensation,
  requirements,
}: {
  compensation: Compensation | null;
  requirements: PostingRequirements | null;
}) {
  if (!requirements) {
    return (
      <p className="mt-3 text-xs text-ink-faint">
        Pay and requirements not read yet —{" "}
        <code className="font-mono">make extract-requirements</code>
      </p>
    );
  }
  const { skills } = requirements;
  const noSkills =
    skills.required.length + skills.preferred.length + skills.unclassified.length === 0;
  return (
    <section aria-label="Pay and requirements" className="mt-4 space-y-2 border-t border-rule-soft pt-3">
      <PayLine pay={compensation} />
      {noSkills ? (
        <p className="text-sm text-ink-faint">No technologies named</p>
      ) : (
        <div className="space-y-1.5">
          <Chips label="requires" tone="text-stop" skills={skills.required} />
          <Chips label="prefers" tone="text-go" skills={skills.preferred} />
          <Chips label="names" tone="text-ink-faint" skills={skills.unclassified} />
        </div>
      )}
      <EducationLine education={requirements.education} />
    </section>
  );
}
