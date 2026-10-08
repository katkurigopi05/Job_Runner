/**
 * What the dock sends of the conversation with the next question.
 *
 * The assistant took one message at a time, so "which of those are remote?"
 * referred to nothing. The last few exchanges now go with the question.
 *
 * This only gathers them. What a model is allowed to see of them is decided
 * by the API (`apps/api/chat_history.py`): an exchange written with recruiter
 * mail in its context does not go to a remote model unless the box is ticked,
 * and the postings an answer cited are read again from the database.
 */

/** One earlier exchange, as `POST /chat` reads it. */
export interface HistoryTurn {
  question: string;
  answer: string;
  mail_in_context: boolean;
  cited: string[];
}

/** The parts of a turn on screen that say whether it belongs to an exchange. */
export interface SpokenTurn {
  role: "you" | "assistant";
  text: string;
  /** Set by the finished reply. An error, or an answer still being written, has none. */
  provider?: string;
  /** Whether recruiter mail was in the context the answer was written from. */
  mailInContext?: boolean;
  sources?: ReadonlyArray<{ posting_id: string; cited: boolean }>;
}

/** How many exchanges go with a question. The API gives the model fewer. */
export const HISTORY_SENT = 6;

/** Whether a model wrote this turn, as an answer to the turn before it. */
function isAnswer(turn: SpokenTurn): boolean {
  // A refusal and a crawl command come from code, and no model was asked.
  return (
    turn.role === "assistant" &&
    turn.provider !== undefined &&
    turn.provider !== "refused" &&
    turn.provider !== "crawler"
  );
}

/** The finished exchanges in a conversation, oldest first, the newest few. */
export function historyFrom(turns: ReadonlyArray<SpokenTurn>): HistoryTurn[] {
  const exchanges: HistoryTurn[] = [];
  for (let index = 1; index < turns.length; index += 1) {
    const asked = turns[index - 1];
    const answered = turns[index];
    if (asked.role !== "you" || !isAnswer(answered)) continue;
    exchanges.push({
      question: asked.text,
      answer: answered.text,
      // Unknown is sent as true: an answer that may quote the mail is treated
      // as one that does.
      mail_in_context: answered.mailInContext ?? true,
      cited: (answered.sources ?? [])
        .filter((source) => source.cited)
        .map((source) => source.posting_id),
    });
  }
  return exchanges.slice(-HISTORY_SENT);
}
