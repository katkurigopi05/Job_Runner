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
  return (
    <ResumeDocumentView
      contact={(document.contact ?? parsed.contact ?? {}) as Record<string, unknown>}
      sections={document.sections ?? {}}
      version={parsed.version}
      lineCount={parsed.line_count}
    />
  );
}

/**
 * The same rendering, from values rather than from a stored résumé.
 *
 * Split out so the editor can feed it a draft. Sharing the component is the
 * whole point: a preview drawn by different code than the saved view would
 * eventually disagree with it, and the one thing this screen must not do is
 * show the owner a document that differs from the one being stored.
 */
export function ResumeDocumentView({
  contact,
  sections,
  version,
  lineCount,
}: {
  contact: Record<string, unknown>;
  sections: Record<string, string[]>;
  /** Omitted for an unsaved draft, which has neither yet. */
  version?: number;
  lineCount?: number;
}) {
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

      {/* A draft has no version and no parsed line count — it has not been
          stored or re-parsed yet. Showing a stale v-number over live text would
          claim the preview is something it is not. */}
      {version !== undefined ? (
        <p className="border-t border-rule px-5 py-3 font-mono text-xs text-ink-faint">
          v{version}
          {lineCount !== undefined ? ` · ${lineCount} lines parsed` : ""}
        </p>
      ) : null}
    </div>
  );
}
