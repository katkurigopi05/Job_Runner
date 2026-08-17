import type { ResumeParsed } from "@/lib/api";

/**
 * The résumé as the parser sees it — which is what actually gets sent.
 *
 * Rendering the parse rather than the original file is the point. A PDF that
 * looks right to a human can still lose a section on the way through an ATS,
 * and the parse is where that shows up. If a heading is missing here, it is
 * missing from the application.
 */
export function ResumePreview({ parsed }: { parsed: ResumeParsed }) {
  const document = parsed.parsed ?? {};
  const sections = document.sections ?? {};
  const contact = (document.contact ?? parsed.contact ?? {}) as Record<string, unknown>;
  const contactBits = ["name", "email", "phone", "location"]
    .map((key) => contact[key])
    .filter((value): value is string => typeof value === "string" && value.trim() !== "");

  const names = Object.keys(sections);

  return (
    <div className="border border-rule bg-paper">
      <div className="border-b border-rule px-5 py-4">
        {contactBits.length > 0 ? (
          <>
            <p className="font-display text-lg">{contactBits[0]}</p>
            <p className="mt-1 font-mono text-xs text-ink-soft">{contactBits.slice(1).join("  ·  ")}</p>
          </>
        ) : (
          <p className="font-mono text-xs text-stop">
            The parser found no contact details. An ATS will not either.
          </p>
        )}
      </div>

      {names.length === 0 ? (
        <p className="px-5 py-6 font-mono text-xs text-stop">
          No sections were recognized. This résumé will not survive an ATS parse — check the
          headings before applying with it.
        </p>
      ) : (
        <div className="divide-y divide-rule">
          {names.map((name) => {
            const lines = sections[name] ?? [];
            return (
              <section key={name} className="px-5 py-4">
                <h4 className="font-mono text-xs uppercase tracking-widest text-ink-faint">
                  {name} <span className="tabular-nums">· {lines.length}</span>
                </h4>
                <ul className="mt-3 space-y-1.5">
                  {lines.map((line, index) => (
                    <li key={`${name}-${index}`} className="text-sm leading-relaxed">
                      {line}
                    </li>
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      )}

      <p className="border-t border-rule px-5 py-3 font-mono text-xs text-ink-faint">
        v{parsed.version} · {parsed.line_count} lines parsed
      </p>
    </div>
  );
}
