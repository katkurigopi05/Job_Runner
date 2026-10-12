"use client";

import { useEffect, useId, useRef, useState } from "react";

/**
 * What can be done with a turn once it is on screen: copy it, and for a
 * question, edit it and ask again.
 *
 * Asked for by the owner on 2026-10-12, after typing one question three ways
 * to get round a wrong answer. Each retyping added two turns to a
 * conversation that is sent back with every later question, so the wrong
 * answer kept being read as context for the next one. Editing takes the
 * question back instead: it is asked again with only what came before it.
 */

/** How long "copied" stands in for "copy". */
const COPIED_FOR_MS = 1500;

/** The longest the editor grows before it scrolls. */
const EDITOR_MAX_ROWS = 6;

/* A verb in the turn's own label line, as quiet as the label. Mono because
   it is a control's name and not prose (globals.css rule 1), and never amber:
   nothing here needs the owner (rule 2). */
const ACTION =
  "font-mono text-xs text-ink-faint underline-offset-2 transition-colors hover:text-ink hover:underline disabled:opacity-40 disabled:hover:text-ink-faint disabled:hover:no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn";

type Copied = "idle" | "copied" | "failed";

const COPY_LABEL: Record<Copied, string> = {
  idle: "copy",
  copied: "copied",
  failed: "copy failed",
};

interface TurnActionsProps {
  role: "you" | "assistant";
  text: string;
  /** False while an answer is being written: the conversation is not settled. */
  canEdit: boolean;
  isEditing: boolean;
  onEdit: () => void;
  /** Told what happened, so the status line can say it to a screen reader. */
  onCopied: (succeeded: boolean) => void;
}

/** " · edit · copy", after the turn's label. */
export function TurnActions({ role, text, canEdit, isEditing, onEdit, onCopied }: TurnActionsProps) {
  const [copied, setCopied] = useState<Copied>("idle");
  const reset = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (reset.current !== null) window.clearTimeout(reset.current);
    },
    [],
  );

  async function handleCopy() {
    let succeeded = true;
    try {
      // Absent outside a secure context, which is the dashboard opened by a
      // LAN address (`JOBRUNNER_WEB_HOST`). That throws too, and is said.
      await navigator.clipboard.writeText(text);
    } catch {
      succeeded = false;
    }
    setCopied(succeeded ? "copied" : "failed");
    onCopied(succeeded);
    if (reset.current !== null) window.clearTimeout(reset.current);
    reset.current = window.setTimeout(() => setCopied("idle"), COPIED_FOR_MS);
  }

  if (isEditing) return <span> · editing</span>;

  const what = role === "you" ? "question" : "answer";
  return (
    <>
      {role === "you" ? (
        <>
          {" · "}
          <button
            type="button"
            disabled={!canEdit}
            onClick={onEdit}
            aria-label="Edit this question and ask again"
            className={ACTION}
          >
            edit
          </button>
        </>
      ) : null}
      {" · "}
      <button type="button" onClick={handleCopy} aria-label={`Copy this ${what}`} className={ACTION}>
        {COPY_LABEL[copied]}
      </button>
    </>
  );
}

interface EditQuestionProps {
  initial: string;
  /** Turns below this question. Asking again removes them, and the form says so. */
  following: number;
  onSend: (text: string) => void;
  onCancel: () => void;
}

/** A question, open for editing in the place its bubble was. */
export function EditQuestion({ initial, following, onSend, onCancel }: EditQuestionProps) {
  const [text, setText] = useState(initial);
  const field = useRef<HTMLTextAreaElement>(null);
  const fieldId = useId();
  const noteId = useId();

  useEffect(() => {
    const node = field.current;
    if (!node) return;
    node.focus();
    node.setSelectionRange(node.value.length, node.value.length);
  }, []);

  const question = text.trim();
  const rows = Math.min(EDITOR_MAX_ROWS, Math.max(2, text.split("\n").length));
  const consequence =
    following > 0
      ? `Asking again removes the ${following} ${following === 1 ? "turn" : "turns"} below it.`
      : "Asking again replaces this question.";

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (question) onSend(question);
      }}
      className="mt-1 ml-auto max-w-[90%] space-y-1.5 text-left"
    >
      <label htmlFor={fieldId} className="sr-only">
        Edit your question
      </label>
      <textarea
        id={fieldId}
        ref={field}
        value={text}
        rows={rows}
        aria-describedby={noteId}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            // The dock closes on Escape. Here it only closes the editor.
            event.stopPropagation();
            onCancel();
          } else if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            if (question) onSend(question);
          }
        }}
        className="block w-full resize-none rounded border border-ink-faint bg-paper px-3 py-2 text-sm leading-relaxed text-ink focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
      />
      {/* On a line of its own: in the dock the editor is 360px wide, and beside
          the two buttons this sentence was squeezed to a word a line. */}
      <p id={noteId} className="text-xs text-ink-faint">
        {consequence}
      </p>
      <div className="flex items-center justify-end gap-3">
        <button type="button" onClick={onCancel} className={ACTION}>
          cancel
        </button>
        <button
          type="submit"
          disabled={!question}
          className="rounded bg-ink px-3 py-1 font-mono text-xs uppercase tracking-widest text-paper transition-opacity hover:opacity-85 disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
        >
          ask again
        </button>
      </div>
    </form>
  );
}
