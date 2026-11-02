"use client";

import { useEffect, useRef, useState } from "react";

const STORAGE_KEY = "jobrunner-side-notes";

export function SideNotes() {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [ready, setReady] = useState(false);
  const [status, setStatus] = useState("");
  const toggle = useRef<HTMLButtonElement>(null);
  const editor = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!open) return;
    editor.current?.focus();
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      setOpen(false);
      toggle.current?.focus();
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [open]);

  useEffect(() => {
    try {
      setNote(localStorage.getItem(STORAGE_KEY) ?? "");
      setReady(true);
    } catch {
      setStatus("Browser storage is unavailable. Reload to try again.");
    }
  }, []);

  function save() {
    try {
      localStorage.setItem(STORAGE_KEY, note);
      setStatus("Saved in this browser.");
    } catch {
      setStatus("Could not save. Keep a copy of your note and try again.");
    }
  }

  return (
    <div className="relative">
      <button
        ref={toggle}
        type="button"
        aria-label="Notes"
        aria-expanded={open}
        aria-controls="side-notes-panel"
        title={open ? "Close notes" : "Open notes"}
        onClick={() => setOpen((current) => !current)}
        className={`flex h-9 w-9 items-center justify-center rounded-[var(--radius)] border border-rule transition-colors hover:border-go hover:text-go focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn ${open ? "bg-paper-high text-ink" : "text-ink-soft"}`}
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          className="h-5 w-5"
        >
          <rect x="4" y="3" width="16" height="18" rx="2" />
          <path d="M8 8h8M8 12h8M8 16h5" strokeLinecap="round" />
        </svg>
      </button>
      <aside
        id="side-notes-panel"
        hidden={!open}
        aria-labelledby="side-notes-title"
        className="absolute right-0 top-full z-40 mt-3 w-80 max-w-[calc(100vw-3rem)] rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-5 shadow-lg"
      >
        <h2 id="side-notes-title" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          Side notes
        </h2>
        <p id="side-notes-help" className="mt-3 text-xs leading-relaxed text-ink-soft">
          Quick reminders, saved only in this browser. Shared across pages.
        </p>
        <label htmlFor="side-notes" className="sr-only">Your notes</label>
        <textarea
          ref={editor}
          id="side-notes"
          aria-describedby="side-notes-help"
          value={note}
          disabled={!ready}
          onChange={(event) => {
            setNote(event.target.value);
            setStatus("Unsaved changes.");
          }}
          rows={6}
          placeholder="Follow up, prepare a question…"
          className="mt-3 block w-full resize-y rounded-[var(--radius)] border border-rule bg-paper p-3 text-sm text-ink placeholder:text-ink-faint focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn disabled:opacity-50"
        />
        <button
          type="button"
          disabled={!ready}
          onClick={save}
          className="mt-3 rounded-[var(--radius)] border border-rule px-3 py-1.5 font-mono text-xs text-ink-soft hover:border-go hover:text-go focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn disabled:opacity-50"
        >
          Save notes
        </button>
        <p role="status" className="mt-2 text-xs leading-relaxed text-ink-faint">{status}</p>
      </aside>
    </div>
  );
}
