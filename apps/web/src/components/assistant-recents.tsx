"use client";

import { useCallback, useId, useMemo, useState, useSyncExternalStore } from "react";

import { Icon } from "@/components/icon";
import {
  type Conversation,
  type KeptTurn,
  type RecentsStore,
  RECENTS_KEY,
  keepIn,
  parseRecents,
  readRecents,
  scopedTo,
  titleOf,
  without,
  writeRecents,
} from "@/lib/chat-recents";
import { deskDateTime } from "@/lib/text";

/**
 * "New chat", and the conversations kept in this browser: open one, delete
 * one, delete all.
 *
 * Asked for by the owner on 2026-11-12. The rules for what is kept are in
 * `lib/chat-recents.ts`. This file is the browser's side of it: the store,
 * and the row above the model picker.
 */

/* The store is read through `useSyncExternalStore`, so two assistants on one
   page and a second tab all show the same list. A write in this tab raises no
   `storage` event, which is what `changed` is for. */
const listeners = new Set<() => void>();

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  window.addEventListener("storage", onChange);
  return () => {
    listeners.delete(onChange);
    window.removeEventListener("storage", onChange);
  };
}

function changed(): void {
  for (const listener of listeners) listener();
}

/** The browser's store, or null where site data is blocked: reading it throws. */
function store(): RecentsStore | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function stored(): string | null {
  try {
    return store()?.getItem(RECENTS_KEY) ?? null;
  } catch {
    return null;
  }
}

export interface Recents<Turn extends KeptTurn> {
  /** Kept where this assistant is (the chat page, or one application's dock), newest first. */
  list: ReadonlyArray<Conversation<Turn>>;
  /** Kept anywhere in this browser. "Delete all" removes this many. */
  total: number;
  /** Save a conversation. False when the browser would not store it. */
  keep: (id: string, turns: Turn[]) => boolean;
  /** Delete one conversation, or with null every one this browser keeps. */
  forget: (id: string | null) => void;
}

export function useRecents<Turn extends KeptTurn>(applicationId: string | undefined): Recents<Turn> {
  const raw = useSyncExternalStore(subscribe, stored, () => null);
  const all = useMemo(() => parseRecents(raw), [raw]);
  // The store is checked for a role and a text on each turn and no further:
  // the rest is what this page wrote there, in this browser, and nobody else.
  const list = useMemo(
    () => scopedTo(all, applicationId) as ReadonlyArray<Conversation<Turn>>,
    [all, applicationId],
  );

  const keep = useCallback(
    (id: string, turns: Turn[]): boolean => {
      const held = store();
      if (!held) return false;
      const outcome = keepIn(held, {
        id,
        title: titleOf(turns),
        updatedAt: new Date().toISOString(),
        applicationId: applicationId ?? null,
        turns,
      });
      changed();
      return outcome.kept;
    },
    [applicationId],
  );

  const forget = useCallback((id: string | null): void => {
    const held = store();
    if (!held) return;
    writeRecents(held, id === null ? [] : without(readRecents(held), id));
    changed();
  }, []);

  return { list, total: all.length, keep, forget };
}

const BUTTON =
  "inline-flex items-center gap-2 rounded-lg px-2 py-1 text-label text-ink-soft transition-colors hover:bg-paper-high hover:text-ink disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-ink-soft focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft";

/* A verb as quiet as the line it sits in, like a turn's "edit" and "copy". */
const VERB =
  "text-xs text-ink-faint underline-offset-2 transition-colors hover:text-ink hover:underline disabled:opacity-40 disabled:hover:no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft";

interface AssistantRecentsProps<Turn extends KeptTurn> {
  recents: Recents<Turn>;
  /** The conversation on screen, so the list can say which one it is. */
  openId: string;
  /** True while an answer is being written: the conversation is not settled. */
  busy: boolean;
  /** True when nothing has been asked yet, so there is no chat to leave. */
  fresh: boolean;
  onNew: () => void;
  onOpen: (conversation: Conversation<Turn>) => void;
  /** Called after the owner confirms. Null is every kept conversation. */
  onDelete: (id: string | null) => void;
}

/** What "delete all" will remove, counting the ones this list does not show. */
function deleteAllLabel(total: number, shown: number): string {
  const elsewhere = total - shown;
  const all = `Delete all ${total}?`;
  return elsewhere > 0 ? `${all} ${elsewhere} of them are kept in other places in the dashboard.` : all;
}

export function AssistantRecents<Turn extends KeptTurn>({
  recents,
  openId,
  busy,
  fresh,
  onNew,
  onOpen,
  onDelete,
}: AssistantRecentsProps<Turn>) {
  const [open, setOpen] = useState(false);
  // What is one click from being deleted: a conversation's id, or "all".
  const [confirming, setConfirming] = useState<string | null>(null);
  const listId = useId();
  const { list, total } = recents;

  function remove(id: string | null) {
    setConfirming(null);
    onDelete(id);
  }

  return (
    <div className="border-b border-rule">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2">
        <button type="button" disabled={busy || fresh} onClick={onNew} className={BUTTON}>
          <Icon name="plus" />
          New chat
        </button>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => {
            setOpen(!open);
            setConfirming(null);
          }}
          className={BUTTON}
        >
          <Icon name="history" />
          Recents{list.length > 0 ? ` · ${list.length}` : ""}
        </button>
        <p className="min-w-0 flex-1 text-right text-xs text-ink-faint">
          Chats are kept in this browser only.
        </p>
      </div>

      {open ? (
        <div id={listId} className="border-t border-rule bg-paper">
          {list.length === 0 ? (
            <p className="px-3 py-3 text-xs text-ink-faint">
              Nothing kept yet. A chat is saved here when its first answer arrives.
            </p>
          ) : (
            <ul className="max-h-48 overflow-y-auto">
              {list.map((kept) => (
                <li
                  key={kept.id}
                  className="flex items-start gap-3 border-b border-rule px-3 py-2 last:border-b-0"
                >
                  <button
                    type="button"
                    disabled={busy}
                    aria-current={kept.id === openId ? "true" : undefined}
                    onClick={() => onOpen(kept)}
                    className="min-w-0 flex-1 text-left disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft"
                  >
                    <span
                      className={`block truncate text-sm ${kept.id === openId ? "text-ink" : "text-ink-soft"} hover:text-ink`}
                    >
                      {kept.title}
                    </span>
                    <span className="block text-xs text-ink-faint">
                      {deskDateTime(kept.updatedAt)}
                      {kept.id === openId ? " · open now" : ""}
                    </span>
                  </button>
                  {confirming === kept.id ? (
                    <span className="shrink-0 text-xs text-ink">
                      Delete this chat?{" "}
                      <button type="button" disabled={busy} onClick={() => remove(kept.id)} className={VERB}>
                        delete
                      </button>
                      {" · "}
                      <button type="button" onClick={() => setConfirming(null)} className={VERB}>
                        keep
                      </button>
                    </span>
                  ) : (
                    <button
                      type="button"
                      disabled={busy}
                      aria-label={`Delete the chat "${kept.title}"`}
                      onClick={() => setConfirming(kept.id)}
                      className={`shrink-0 ${VERB}`}
                    >
                      delete
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
          {total > 0 ? (
            <p className="border-t border-rule px-3 py-2 text-right text-xs text-ink-faint">
              {confirming === "all" ? (
                <span className="text-ink">
                  {deleteAllLabel(total, list.length)}{" "}
                  <button type="button" disabled={busy} onClick={() => remove(null)} className={VERB}>
                    delete all
                  </button>
                  {" · "}
                  <button type="button" onClick={() => setConfirming(null)} className={VERB}>
                    keep
                  </button>
                </span>
              ) : (
                <button type="button" disabled={busy} onClick={() => setConfirming("all")} className={VERB}>
                  delete all
                </button>
              )}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
