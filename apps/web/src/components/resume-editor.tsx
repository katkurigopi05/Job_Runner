"use client";

import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";
import type { ResumeParsed } from "@/lib/api";
import { ResumeDocumentView } from "@/components/resume-preview";
import { saveResumeEdit } from "@/app/resumes/actions";
import type { UploadResult } from "@/app/resumes/actions";

/**
 * Edit the parsed résumé in place.
 *
 * The parsed form was read-only, so fixing a typo — or a section the parser
 * mis-split — meant editing the source document elsewhere and re-uploading.
 *
 * One textarea per section, one line per bullet, because that is the shape the
 * parser produced and the shape tailoring consumes. A richer editor would have
 * to invent a mapping between what the owner sees and what the guard checks
 * against, and that mapping is exactly where a résumé stops meaning what it
 * says.
 *
 * Saving creates a **new version**. The one on screen may already have been
 * sent to an employer, and an application's receipt has to keep describing the
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

export function ResumeEditor({ parsed }: { parsed: ResumeParsed }) {
  const [open, setOpen] = useState(false);
  const document = parsed.parsed ?? {};
  const saved = (document.contact ?? {}) as Record<string, unknown>;
  const savedSections = document.sections ?? {};

  const action = saveResumeEdit.bind(null, parsed.id);
  const [state, run] = useActionState<UploadResult | null, FormData>(action, null);

  const text = (value: unknown) => (typeof value === "string" ? value : "");
  const links = Array.isArray(saved.links) ? (saved.links as string[]) : [];

  // Draft state, so the preview can show what is being typed rather than what
  // was last saved. Controlled inputs rather than `defaultValue`: an
  // uncontrolled form cannot drive a preview, and a preview fed from anywhere
  // other than the same values the form will submit would eventually disagree
  // with what gets stored.
  const [name, setName] = useState(text(saved.name));
  const [email, setEmail] = useState(text(saved.email));
  const [phone, setPhone] = useState(text(saved.phone));
  const [linkText, setLinkText] = useState(links.join("\n"));
  const [drafts, setDrafts] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(savedSections).map(([key, lines]) => [key, lines.join("\n")])),
  );

  // What the server will build from this form, computed the same way it will:
  // blank lines dropped, empty sections removed. Showing the raw textareas
  // instead would preview a document that is not the one being saved.
  const draftContact: Record<string, unknown> = { name, email, phone };
  const draftSections = Object.fromEntries(
    Object.entries(drafts)
      .map(([key, value]) => [key, value.split("\n").filter((line) => line.trim() !== "")])
      .filter(([, lines]) => (lines as string[]).length > 0),
  ) as Record<string, string[]>;

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="rounded border border-rule px-3 py-1.5 font-mono text-xs text-ink-soft transition-colors hover:border-ink-faint hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
      >
        edit
      </button>
    );
  }

  const field =
    "mt-1 w-full rounded border border-rule bg-paper-raised px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn";
  const legend = "font-mono text-xs uppercase tracking-widest text-ink-soft";

  return (
    <form action={run} className="mt-5 border border-rule bg-paper">
      {/* Form and preview side by side on a wide screen, stacked on a narrow
          one. The preview is the same component the saved résumé renders
          through, fed the draft instead — drawing it with different code would
          eventually disagree with what actually gets stored, and this screen
          exists precisely so the owner can trust what they are looking at. */}
      <div className="grid gap-6 p-5 lg:grid-cols-2">
        <div className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-3">
            <label className="block">
              <span className={legend}>name</span>
              <input
                name="contact:name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                className={field}
              />
            </label>
            <label className="block">
              <span className={legend}>email</span>
              <input
                name="contact:email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                className={field}
              />
            </label>
            <label className="block">
              <span className={legend}>phone</span>
              <input
                name="contact:phone"
                value={phone}
                onChange={(event) => setPhone(event.target.value)}
                className={field}
              />
            </label>
          </div>

          <label className="block">
            <span className={legend}>links — one per line</span>
            <textarea
              name="contact:links"
              rows={2}
              value={linkText}
              onChange={(event) => setLinkText(event.target.value)}
              className={`${field} font-mono`}
            />
          </label>

          {Object.entries(drafts).map(([section, value]) => (
            <label key={section} className="block">
              <span className={legend}>{section} — one line per bullet</span>
              <textarea
                name={`section:${section}`}
                rows={Math.min(Math.max(value.split("\n").length + 1, 3), 18)}
                value={value}
                onChange={(event) =>
                  setDrafts((held) => ({ ...held, [section]: event.target.value }))
                }
                className={`${field} leading-relaxed`}
              />
            </label>
          ))}
        </div>

        <div className="lg:sticky lg:top-6 lg:self-start">
          <p className={`${legend} mb-2`}>preview — as the parser will read it</p>
          <ResumeDocumentView contact={draftContact} sections={draftSections} />
          <p className="mt-2 font-mono text-xs text-ink-faint">
            Not saved yet. A section that empties disappears here because it will not be written.
          </p>
        </div>
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
        <p className="w-full font-mono text-xs text-ink-faint">
          Saving renders a new PDF and points every profile that used this résumé at it. The
          version on screen is kept — an application that already sent it keeps describing what it
          sent.
        </p>
      </div>
    </form>
  );
}
