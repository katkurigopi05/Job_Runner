"use client";

import { Icon } from "@/components/icon";
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
    <div className="side-notes">
      <button
        ref={toggle}
        type="button"
        aria-label="Notes"
        aria-expanded={open}
        aria-controls="side-notes-panel"
        title={open ? "Close notes" : "Open notes"}
        onClick={() => setOpen((current) => !current)}
        className="side-notes-toggle"
      >
        <Icon name="notes" />Notes
      </button>
      <aside
        id="side-notes-panel"
        hidden={!open}
        aria-labelledby="side-notes-title"
        className="side-notes-panel rounded-[var(--radius-panel)] border border-rule bg-paper-raised p-5 shadow-lg"
      >
        <h2 id="side-notes-title" className="text-label text-ink-soft">
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
          className="mt-3 block w-full resize-y rounded-[var(--radius)] border border-rule bg-paper p-3 text-sm text-ink placeholder:text-ink-faint focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft disabled:opacity-50"
        />
        <button
          type="button"
          disabled={!ready}
          onClick={save}
          className="mt-3 rounded-[var(--radius)] border border-rule px-3 py-1.5 text-xs text-ink-soft hover:border-ink-soft hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft disabled:opacity-50"
        >
          Save notes
        </button>
        <p role="status" className="mt-2 text-xs leading-relaxed text-ink-faint">{status}</p>
      </aside>
    </div>
  );
}
