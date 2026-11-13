/**
 * The assistant's kept conversations: "Recents".
 *
 * Asked for by the owner on 2026-11-12. A conversation was the open page's
 * state, so "New conversation" or a reload discarded it and nothing could
 * bring it back. Each finished answer now saves the conversation it belongs
 * to, a new chat leaves the last one in the list, and the owner deletes one
 * or all of them.
 *
 * Kept in this browser's `localStorage` and nowhere else: no request carries
 * a kept conversation, and the API stores none. What is kept is the turns as
 * they were on screen, so a reopened conversation is remembered by the same
 * rules as an open one (`chat-history.ts`, `apps/api/chat_history.py`). The
 * model picker and the recruiter-mail box are not kept: both are decided
 * again for each question.
 *
 * Pure but for the three functions that take a `Storage`, which is passed in
 * so Node can run all of it (`tests/chat-recents.test.mjs`).
 */

/** The parts of a turn a kept conversation is checked for. The rest rides along. */
export interface KeptTurn {
  role: "you" | "assistant";
  text: string;
}

export interface Conversation<Turn extends KeptTurn = KeptTurn> {
  id: string;
  /** The first question, cut short. */
  title: string;
  /** When it last changed, as an ISO timestamp. */
  updatedAt: string;
  /** The application whose dock it was held in, or null for the chat page. */
  applicationId: string | null;
  turns: Turn[];
}

/** What a browser's `localStorage` is asked for here. */
export type RecentsStore = Pick<Storage, "getItem" | "setItem">;

/** Versioned: a later shape reads nothing of this one instead of misreading it. */
export const RECENTS_KEY = "jobrunner.chat.recents.v1";

/** How many conversations are kept. The oldest goes when one more is saved. */
export const MAX_RECENTS = 50;

/**
 * How much of the store's room they may take, in characters. A browser gives
 * an origin about five million; an answer that lists every match of a broad
 * role is some hundred thousand.
 */
export const MAX_STORED_CHARS = 3_000_000;

const TITLE_LENGTH = 60;

/** What a conversation is called in the list: its first question, on one line. */
export function titleOf(turns: ReadonlyArray<KeptTurn>): string {
  const asked = turns.find((turn) => turn.role === "you")?.text ?? "";
  const line = asked.replace(/\s+/g, " ").trim();
  if (!line) return "Untitled chat";
  return line.length > TITLE_LENGTH ? `${line.slice(0, TITLE_LENGTH - 1)}…` : line;
}

/** A fresh id. `randomUUID` needs localhost or https, so there is a plain fallback. */
export function newChatId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * The list with this conversation saved, newest first.
 *
 * The same list comes back when its turns have not changed: opening a kept
 * conversation is not a change to it and must not move it to the top.
 */
export function withSaved<Turn extends KeptTurn>(
  list: ReadonlyArray<Conversation<Turn>>,
  conversation: Conversation<Turn>,
): ReadonlyArray<Conversation<Turn>> {
  const held = list.find((kept) => kept.id === conversation.id);
  if (held && JSON.stringify(held.turns) === JSON.stringify(conversation.turns)) return list;
  return [conversation, ...list.filter((kept) => kept.id !== conversation.id)].slice(0, MAX_RECENTS);
}

/** The list without one conversation. */
export function without<Turn extends KeptTurn>(
  list: ReadonlyArray<Conversation<Turn>>,
  id: string,
): ReadonlyArray<Conversation<Turn>> {
  return list.filter((kept) => kept.id !== id);
}

/** The conversations held where this assistant is: one application's dock, or the chat page. */
export function scopedTo<Turn extends KeptTurn>(
  list: ReadonlyArray<Conversation<Turn>>,
  applicationId: string | undefined,
): ReadonlyArray<Conversation<Turn>> {
  return list.filter((kept) => kept.applicationId === (applicationId ?? null));
}

/** The newest conversations that fit in `limit` characters. The oldest go first. */
export function fitted<Turn extends KeptTurn>(
  list: ReadonlyArray<Conversation<Turn>>,
  limit: number = MAX_STORED_CHARS,
): ReadonlyArray<Conversation<Turn>> {
  let kept = list;
  while (kept.length > 0 && JSON.stringify(kept).length > limit) kept = kept.slice(0, -1);
  return kept;
}

function isTurn(value: unknown): value is KeptTurn {
  if (typeof value !== "object" || value === null) return false;
  const turn = value as Record<string, unknown>;
  return (turn.role === "you" || turn.role === "assistant") && typeof turn.text === "string";
}

function isConversation(value: unknown): value is Conversation {
  if (typeof value !== "object" || value === null) return false;
  const kept = value as Record<string, unknown>;
  return (
    typeof kept.id === "string" &&
    typeof kept.title === "string" &&
    typeof kept.updatedAt === "string" &&
    !Number.isNaN(Date.parse(kept.updatedAt)) &&
    (kept.applicationId === null || typeof kept.applicationId === "string") &&
    Array.isArray(kept.turns) &&
    kept.turns.every(isTurn)
  );
}

/**
 * What the store holds, read as data that anything may have written.
 *
 * An entry that is not a conversation is dropped and the rest are kept: one
 * bad row must not cost the owner the others.
 */
export function parseRecents(raw: string | null): Conversation[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(isConversation) : [];
  } catch {
    return [];
  }
}

/** The kept conversations. A store that cannot be read holds none. */
export function readRecents(store: RecentsStore): Conversation[] {
  try {
    return parseRecents(store.getItem(RECENTS_KEY));
  } catch {
    // Private windows and blocked site data throw on access.
    return [];
  }
}

/** Whether the list was stored. A full or blocked store refuses. */
export function writeRecents(store: RecentsStore, list: ReadonlyArray<Conversation>): boolean {
  try {
    store.setItem(RECENTS_KEY, JSON.stringify(list));
    return true;
  } catch {
    return false;
  }
}

/** What saving came to: whether it is stored, and what the store now holds. */
export interface Kept {
  kept: boolean;
  list: ReadonlyArray<Conversation>;
}

/**
 * Save one conversation into the store, read first so another tab's are not
 * written over. When it cannot be stored, what was held is left as it was.
 */
export function keepIn(store: RecentsStore, conversation: Conversation): Kept {
  const held = readRecents(store);
  if (conversation.turns.length === 0) return { kept: false, list: held };
  const next = fitted(withSaved(held, conversation));
  if (next === held) return { kept: true, list: held };
  const fits = next.some((kept) => kept.id === conversation.id);
  if (!fits || !writeRecents(store, next)) return { kept: false, list: held };
  return { kept: true, list: next };
}
