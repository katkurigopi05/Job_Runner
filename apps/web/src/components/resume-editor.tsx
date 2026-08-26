"use client";

import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";
import type { ResumeParsed } from "@/lib/api";
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
  const contact = (document.contact ?? {}) as Record<string, unknown>;
  const sections = document.sections ?? {};

  const action = saveResumeEdit.bind(null, parsed.id);
  const [state, run] = useActionState<UploadResult | null, FormData>(action, null);

  const text = (value: unknown) => (typeof value === "string" ? value : "");
  const links = Array.isArray(contact.links) ? (contact.links as string[]) : [];

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

  return (
    <form action={run} className="mt-5 space-y-6 border border-rule bg-paper p-5">
      <div className="grid gap-4 sm:grid-cols-3">
        <label className="block">
          <span className="font-mono text-xs uppercase tracking-widest text-ink-soft">name</span>
          <input
            name="contact:name"
            defaultValue={text(contact.name)}
            className="mt-1 w-full rounded border border-rule bg-paper-raised px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
          />
        </label>
        <label className="block">
          <span className="font-mono text-xs uppercase tracking-widest text-ink-soft">email</span>
          <input
            name="contact:email"
            defaultValue={text(contact.email)}
            className="mt-1 w-full rounded border border-rule bg-paper-raised px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
          />
        </label>
        <label className="block">
          <span className="font-mono text-xs uppercase tracking-widest text-ink-soft">phone</span>
          <input
            name="contact:phone"
            defaultValue={text(contact.phone)}
            className="mt-1 w-full rounded border border-rule bg-paper-raised px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
          />
        </label>
      </div>

      <label className="block">
        <span className="font-mono text-xs uppercase tracking-widest text-ink-soft">
          links — one per line
        </span>
        <textarea
          name="contact:links"
          rows={2}
          defaultValue={links.join("\n")}
          className="mt-1 w-full rounded border border-rule bg-paper-raised px-3 py-2 font-mono text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
        />
      </label>

      {Object.entries(sections).map(([name, lines]) => (
        <label key={name} className="block">
          <span className="font-mono text-xs uppercase tracking-widest text-ink-soft">
            {name} — one line per bullet
          </span>
          <textarea
            name={`section:${name}`}
            rows={Math.min(Math.max(lines.length + 1, 3), 18)}
            defaultValue={lines.join("\n")}
            className="mt-1 w-full rounded border border-rule bg-paper-raised px-3 py-2 text-sm leading-relaxed focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
          />
        </label>
      ))}

      <p className="font-mono text-xs text-ink-faint">
        Saving renders a new PDF and points every profile that used this résumé at it. The version
        on screen is kept — an application that already sent it keeps describing what it sent.
      </p>

      <div className="flex flex-wrap items-center gap-4">
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
      </div>
    </form>
  );
}
