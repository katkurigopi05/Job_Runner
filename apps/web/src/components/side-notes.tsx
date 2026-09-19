"use client";

import { useEffect, useState } from "react";

const STORAGE_KEY = "jobrunner-side-notes";

export function SideNotes() {
  const [note, setNote] = useState("");
  const [ready, setReady] = useState(false);
  const [status, setStatus] = useState("");

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
    <aside
      aria-labelledby="side-notes-title"
      className="h-fit rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-5"
    >
      <h2 id="side-notes-title" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
        Side notes
      </h2>
      <p id="side-notes-help" className="mt-3 text-xs leading-relaxed text-ink-soft">
        Quick reminders, saved only in this browser. Shared across pages.
      </p>
      <label htmlFor="side-notes" className="sr-only">Your notes</label>
      <textarea
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
  );
}
