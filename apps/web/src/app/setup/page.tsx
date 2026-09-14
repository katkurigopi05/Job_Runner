import { ApiError, api, type SetupItem, type SetupState, type SetupStatus } from "@/lib/api";
import { RegistrySync } from "./registry-sync";
import { StepList } from "./step-list";

export const dynamic = "force-dynamic";

/**
 * Setup and recovery: every part this installation needs, whether it has it,
 * and the exact step that fixes it.
 *
 * Built because the audit found the causes of an empty feed — a registry out
 * of step with its database, an invalid vault key, an unconfigured inbox — in
 * a terminal, while the dashboard showing the symptoms said nothing about
 * them. No value on this page is a secret: the API builds it from diagnostics
 * that refuse to repeat one.
 */

const GROUPS: { key: string; title: string; blurb: string }[] = [
  { key: "core", title: "Core services", blurb: "Nothing works without these." },
  {
    key: "credentials",
    title: "Credentials",
    blurb: "Stored ATS logins and the mailbox replies arrive in.",
  },
  { key: "discovery", title: "Discovery", blurb: "Where new postings come from." },
  { key: "tools", title: "Local tools", blurb: "Rendering, the guard, the browser, models." },
];

const STATE: Record<SetupState, { label: string; tone: string; rule: string; rank: number }> = {
  blocked: { label: "blocked", tone: "text-stop", rule: "border-l-stop", rank: 0 },
  attention: { label: "needs attention", tone: "text-attn", rule: "border-l-attn", rank: 1 },
  unknown: { label: "unknown", tone: "text-ink-faint", rule: "border-l-rule", rank: 2 },
  ok: { label: "ok", tone: "text-go", rule: "border-l-go", rank: 3 },
};

const HEADLINE: Record<SetupState, string> = {
  ok: "Everything this installation needs is in place.",
  unknown: "Some checks could not be completed.",
  attention: "Working, with problems worth fixing.",
  blocked: "Something is stopping part of the job search.",
};

function humanize(key: string): string {
  return key.replaceAll("_", " ");
}

function Facts({ facts }: { facts: SetupItem["facts"] }) {
  const entries = Object.entries(facts).filter(([key, value]) => value !== null && key !== "required");
  if (entries.length === 0) return null;
  return (
    <dl className="mt-4 grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-4 gap-y-1 text-xs sm:grid-cols-[minmax(0,auto)_minmax(0,1fr)_minmax(0,auto)_minmax(0,1fr)]">
      {entries.map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="font-mono text-ink-faint">{humanize(key)}</dt>
          <dd className="tabular-nums text-ink-soft">{String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Item({ item }: { item: SetupItem }) {
  const state = STATE[item.state];
  return (
    <article
      id={item.key}
      className={`scroll-mt-6 rounded-[var(--radius)] border border-rule border-l-4 ${state.rule} bg-paper-raised px-5 py-4`}
    >
      <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="font-display text-lg leading-tight">{item.title}</h3>
        <span className={`font-mono text-xs uppercase tracking-widest ${state.tone}`}>
          {state.label}
        </span>
      </header>
      <p className="mt-2 max-w-prose text-sm leading-relaxed text-ink-soft">{item.detail}</p>
      {item.steps.length > 0 ? <StepList steps={item.steps} /> : null}
      {item.actions.includes("registry_sync") ? <RegistrySync /> : null}
      <Facts facts={item.facts} />
    </article>
  );
}

function Unreachable({ error }: { error: ApiError }) {
  return (
    <section className="border border-stop/40 bg-stop-soft px-6 py-6">
      <h1 className="font-display text-2xl text-stop">The API is not answering</h1>
      <p className="mt-3 max-w-prose text-ink-soft">
        This page is built by the API, so with it down the only check left is the one you run.
      </p>
      <StepList steps={["make up      # Postgres", "make api     # http://127.0.0.1:8000", "make doctor"]} />
      <p className="mt-5 font-mono text-xs text-ink-faint">
        {error.code} · {error.message}
      </p>
    </section>
  );
}

export default async function SetupPage() {
  let status: SetupStatus;
  try {
    status = await api.setupStatus();
  } catch (error) {
    if (error instanceof ApiError) return <Unreachable error={error} />;
    throw error;
  }

  const open = status.items
    .filter((item) => item.state !== "ok")
    .sort((a, b) => STATE[a.state].rank - STATE[b.state].rank);
  const generated = new Date(status.generated_at).toLocaleString();

  return (
    <div className="space-y-12">
      <header>
        <p className={`font-mono text-xs uppercase tracking-widest ${STATE[status.overall].tone}`}>
          setup · {STATE[status.overall].label}
        </p>
        <h1 className="mt-3 max-w-3xl font-display text-4xl leading-[1.05] tracking-tight sm:text-5xl">
          {HEADLINE[status.overall]}
        </h1>
        <p className="mt-4 text-xs text-ink-faint">Checked {generated}. Reload to check again.</p>

        {open.length > 0 ? (
          <nav aria-label="Problems" className="mt-8 border-t border-rule pt-5">
            <h2 className="font-mono text-xs uppercase tracking-widest text-ink-faint">
              {open.length} to look at, worst first
            </h2>
            <ul className="mt-3 flex flex-wrap gap-2">
              {open.map((item) => (
                <li key={item.key}>
                  <a
                    href={`#${item.key}`}
                    className="inline-flex items-baseline gap-2 rounded-[var(--radius)] border border-rule px-3 py-1.5 text-sm transition-colors hover:border-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
                  >
                    <span className={`h-2 w-2 translate-y-[-1px] rounded-full ${STATE[item.state].tone.replace("text-", "bg-")}`} />
                    {item.title}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        ) : null}
      </header>

      {GROUPS.map((group) => {
        const items = status.items.filter((item) => item.group === group.key);
        if (items.length === 0) return null;
        return (
          <section key={group.key} aria-labelledby={`group-${group.key}`}>
            <div className="flex flex-wrap items-baseline gap-x-4 border-b border-rule pb-2">
              <h2 id={`group-${group.key}`} className="font-display text-2xl">
                {group.title}
              </h2>
              <p className="text-sm text-ink-faint">{group.blurb}</p>
            </div>
            <div className="mt-5 space-y-4">
              {items.map((item) => (
                <Item key={item.key} item={item} />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}
