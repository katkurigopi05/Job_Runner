"use client";

import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";
import type { ResumeParsed } from "@/lib/api";
import { ResumePreview } from "@/components/resume-preview";
import { saveResumeEdit } from "@/app/resumes/actions";
import type { UploadResult } from "@/app/resumes/actions";

/**
 * Edit the résumé inside the document, rather than in a form beside it.
 *
 * The first version put a form on the left and a preview on the right, which
 * meant reading the same résumé twice in two shapes and mapping between them
 * yourself. Here the preview *is* the editing surface: every line sits where it
 * will appear, and typing changes it in place. There is nothing to map, because
 * there is only one rendering.
 *
 * Lines stay one-per-bullet because that is the shape the parser produced and
 * the shape tailoring consumes. A richer editor would need a mapping between
 * what the owner sees and what the fabrication guard checks against, and that
 * mapping is exactly where a résumé stops meaning what it says.
 *
 * Saving creates a **new version**. The one on screen may already have gone to
 * an employer, and that application's receipt has to keep describing the
 * document that actually went.
 */
function Saving() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="rounded-md bg-go px-5 py-2.5 font-mono text-xs uppercase tracking-widest text-paper transition-colors hover:bg-go/85 disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-go"
    >
      {pending ? "saving…" : "save as new version"}
    </button>
  );
}

/** A line of the document you can type into, sized to its own content. */
function EditableLine({
  value,
  onChange,
  onRemove,
  placeholder,
  className = "",
}: {
  value: string;
  onChange: (next: string) => void;
  onRemove?: () => void;
  placeholder?: string;
  className?: string;
}) {
  const fit = (node: HTMLTextAreaElement | null) => {
    if (!node) return;
    // Grow with the text. A fixed-height box mid-document hides the end of a
    // bullet, which is the part most likely to be wrong after tailoring.
    node.style.height = "auto";
    node.style.height = `${node.scrollHeight}px`;
  };

  return (
    <div className="group flex items-start gap-2">
      <textarea
        rows={1}
        value={value}
        placeholder={placeholder}
        ref={fit}
        onChange={(event) => {
          onChange(event.target.value);
          fit(event.target);
        }}
        className={`w-full resize-none rounded border border-transparent bg-transparent px-1.5 py-0.5 leading-relaxed hover:border-rule focus:border-attn focus-visible:outline-none ${className}`}
      />
      {onRemove ? (
        <button
          type="button"
          onClick={onRemove}
          aria-label="Remove this line"
          className="mt-1 shrink-0 rounded px-1 font-mono text-xs text-ink-faint opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 hover:text-stop focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-stop"
        >
          ✕
        </button>
      ) : null}
    </div>
  );
}

export function ResumeEditor({
  parsed,
  action,
  note,
  extra,
  editLabel = "edit",
}: {
  parsed: ResumeParsed;
  /**
   * What saving does. Defaults to the résumés-page save, which versions the
   * document and moves every profile onto it.
   *
   * The review screen passes its own: there an edit belongs to one application
   * and must not move the profile's base. Taking the action as a prop rather
   * than forking the component is deliberate — this editor *is* the preview
   * (see above), and a second copy of it would be a second rendering of the
   * résumé that could disagree with the one being stored.
   */
  action?: (prev: UploadResult | null, form: FormData) => Promise<UploadResult>;
  /** Replaces the footer explanation, which differs by what saving means. */
  note?: React.ReactNode;
  /** Extra controls posted with the form — the review screen's adopt opt-in. */
  extra?: React.ReactNode;
  editLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const document = parsed.parsed ?? {};
  const saved = (document.contact ?? {}) as Record<string, unknown>;
  const savedSections = document.sections ?? {};

  const save = action ?? saveResumeEdit.bind(null, parsed.id);
  const [state, run] = useActionState<UploadResult | null, FormData>(save, null);

  const text = (value: unknown) => (typeof value === "string" ? value : "");
  const savedLinks = Array.isArray(saved.links) ? (saved.links as string[]) : [];

  const [name, setName] = useState(text(saved.name));
  const [email, setEmail] = useState(text(saved.email));
  const [phone, setPhone] = useState(text(saved.phone));
  const [linkText, setLinkText] = useState(savedLinks.join("\n"));
  // Per line rather than per section, because the document edits a line at a
  // time and a section-sized textarea is the thing this replaces.
  const [sections, setSections] = useState<Record<string, string[]>>(() =>
    Object.fromEntries(Object.entries(savedSections).map(([key, lines]) => [key, [...lines]])),
  );

  const setLine = (section: string, index: number, next: string) =>
    setSections((held) => ({
      ...held,
      [section]: held[section].map((line, i) => (i === index ? next : line)),
    }));

  const removeLine = (section: string, index: number) =>
    setSections((held) => ({
      ...held,
      [section]: held[section].filter((_, i) => i !== index),
    }));

  const addLine = (section: string) =>
    setSections((held) => ({ ...held, [section]: [...held[section], ""] }));

  // Closed, this *is* the preview — the saved document, with a way into it.
  // Rendering the preview separately alongside would put two copies of the same
  // résumé on the page and leave the owner deciding which one is real.
  if (!open) {
    return (
      <div className="relative">
        <ResumePreview parsed={parsed} />
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="absolute top-3 right-3 rounded border border-rule bg-paper-raised px-3 py-1.5 font-mono text-xs text-ink-soft transition-colors hover:border-ink-faint hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
        >
          {editLabel}
        </button>
      </div>
    );
  }

  const legend = "font-mono text-xs uppercase tracking-widest text-ink-faint";

  return (
    <form action={run} className="border border-attn/40 bg-paper">
      {/* What the server actually receives, bound to the same state the visible
          document is. Two sources of truth here would mean a document that
          reads correctly on screen and arrives at an employer different, and
          nothing on the page would show it.

          Joined here rather than split server-side so `saveResumeEdit` keeps
          the contract it already had: a section is newline-separated text. */}
      {Object.entries(sections).map(([section, lines]) => (
        <input key={section} type="hidden" name={`section:${section}`} value={lines.join("\n")} />
      ))}
      <input type="hidden" name="contact:name" value={name} />
      <input type="hidden" name="contact:email" value={email} />
      <input type="hidden" name="contact:phone" value={phone} />
      <input type="hidden" name="contact:links" value={linkText} />

      <header className="flex flex-wrap items-baseline justify-between gap-3 border-b border-rule px-5 py-3">
        <p className={legend}>editing — this is the document, type in it</p>
        <p className="font-mono text-xs text-ink-faint">
          from v{parsed.version} · saves as a new version
        </p>
      </header>

      {/* Contact laid out as it reads on the résumé rather than as form fields,
          so the top of the editor is the top of the document. */}
      <div className="border-b border-rule px-5 py-4">
        <EditableLine
          value={name}
          onChange={setName}
          placeholder="Your name"
          className="font-display text-lg"
        />
        <div className="mt-1 grid gap-1 sm:grid-cols-2">
          <EditableLine
            value={email}
            onChange={setEmail}
            placeholder="email"
            className="font-mono text-xs text-ink-soft"
          />
          <EditableLine
            value={phone}
            onChange={setPhone}
            placeholder="phone"
            className="font-mono text-xs text-ink-soft"
          />
        </div>
        <div className="mt-1">
          <EditableLine
            value={linkText}
            onChange={setLinkText}
            placeholder="links — one per line"
            className="font-mono text-xs text-ink-soft"
          />
        </div>
      </div>

      <div className="divide-y divide-rule">
        {Object.entries(sections).map(([section, lines]) => (
          <section key={section} className="px-5 py-4">
            <h4 className={legend}>
              {section}{" "}
              <span className="tabular-nums">· {lines.filter((l) => l.trim()).length}</span>
            </h4>
            <div className="mt-3 space-y-1">
              {lines.map((line, index) => (
                <EditableLine
                  key={`${section}-${index}`}
                  value={line}
                  onChange={(next) => setLine(section, index, next)}
                  onRemove={() => removeLine(section, index)}
                  placeholder="empty — this line will be dropped"
                  className="text-sm"
                />
              ))}
            </div>
            <button
              type="button"
              onClick={() => addLine(section)}
              className="mt-2 rounded border border-rule px-2 py-1 font-mono text-xs text-ink-soft transition-colors hover:border-ink-faint hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
            >
              + line
            </button>
          </section>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-4 border-t border-rule px-5 py-4">
        <Saving />
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="font-mono text-xs text-ink-soft underline-offset-4 hover:text-ink hover:underline"
        >
          cancel
        </button>
        {state ? (
          <span className={`text-sm ${state.ok ? "text-go" : "text-stop"}`}>{state.message}</span>
        ) : null}
        {extra ? <div className="w-full">{extra}</div> : null}
        <p className="w-full font-mono text-xs text-ink-faint">
          {note ?? (
            <>
              Blank lines are dropped and an emptied section is removed, so what gets saved is what
              you see with the empties gone. Saving renders a new PDF and points every profile that
              used this résumé at it — the version on screen is kept, because an application that
              already sent it must keep describing what it sent.
            </>
          )}
        </p>
      </div>
    </form>
  );
}
