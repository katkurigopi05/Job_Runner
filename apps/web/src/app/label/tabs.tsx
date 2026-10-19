import Link from "next/link";

/** The two things graded here: postings for a profile, and search answers to a question. */
export function GradeTabs({ active }: { active: "postings" | "questions" }) {
  const tabs = [
    { key: "postings", href: "/label", label: "Postings", hint: "is this a job for me" },
    {
      key: "questions",
      href: "/label/questions",
      label: "Search answers",
      hint: "is this what I asked",
    },
  ] as const;
  return (
    <nav aria-label="What to grade" className="flex flex-wrap gap-2">
      {tabs.map((tab) => (
        <Link
          key={tab.key}
          href={tab.href}
          aria-current={tab.key === active ? "page" : undefined}
          className={
            tab.key === active
              ? "rounded-[var(--radius)] border border-ink bg-surface px-3 py-2"
              : "rounded-[var(--radius)] border border-rule px-3 py-2 text-ink-soft hover:border-ink hover:text-ink"
          }
        >
          <span className="font-mono text-sm">{tab.label}</span>
          <span className="ml-2 text-xs text-ink-faint">{tab.hint}</span>
        </Link>
      ))}
    </nav>
  );
}
