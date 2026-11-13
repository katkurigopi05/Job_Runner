// The assistant's kept conversations ("Recents"), on Node's own test runner.
// Until 2026-11-12 a conversation was the open page's: "New conversation" or
// a reload discarded it and nothing could bring it back.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  MAX_RECENTS,
  RECENTS_KEY,
  fitted,
  keepIn,
  parseRecents,
  readRecents,
  scopedTo,
  titleOf,
  withSaved,
  without,
  writeRecents,
} from "../src/lib/chat-recents.ts";

const ASKED = { role: "you", text: "Which open roles use Kafka?" };
const ANSWERED = { role: "assistant", text: "Five do.", provider: "ollama", mailInContext: false };

function conversation(id, overrides = {}) {
  return {
    id,
    title: "Which open roles use Kafka?",
    updatedAt: "2026-11-12T20:00:00.000Z",
    applicationId: null,
    turns: [ASKED, ANSWERED],
    ...overrides,
  };
}

/** A `Storage` that holds what it is given, or refuses like a full one. */
function storage(initial = {}, { full = false } = {}) {
  const held = { ...initial };
  return {
    held,
    getItem: (key) => (key in held ? held[key] : null),
    setItem: (key, value) => {
      if (full) throw new Error("QuotaExceededError");
      held[key] = value;
    },
  };
}

describe("titleOf", () => {
  test("is the first question, on one line", () => {
    assert.equal(titleOf([{ role: "you", text: "  any\n replies? " }, ANSWERED]), "any replies?");
  });

  test("cuts a long question and says it was cut", () => {
    const title = titleOf([{ role: "you", text: "x".repeat(200) }]);
    assert.equal(title.length, 60);
    assert.ok(title.endsWith("…"));
  });

  test("names a conversation that has no question", () => {
    assert.equal(titleOf([]), "Untitled chat");
  });
});

describe("withSaved", () => {
  test("puts a new conversation first", () => {
    const list = withSaved([conversation("a")], conversation("b"));
    assert.deepEqual(list.map((kept) => kept.id), ["b", "a"]);
  });

  test("replaces a conversation that grew, and moves it first", () => {
    const grown = conversation("a", { turns: [ASKED, ANSWERED, ASKED, ANSWERED] });
    const list = withSaved([conversation("b"), conversation("a")], grown);
    assert.deepEqual(list.map((kept) => kept.id), ["a", "b"]);
    assert.equal(list[0].turns.length, 4);
  });

  test("leaves the list alone when the conversation has not changed", () => {
    // Opening a kept conversation must not make it the most recent one.
    const list = [conversation("b"), conversation("a")];
    const later = conversation("a", { updatedAt: "2026-11-13T09:00:00.000Z" });
    assert.equal(withSaved(list, later), list);
  });

  test("does not change the list it was given", () => {
    const list = [conversation("a")];
    withSaved(list, conversation("b"));
    assert.deepEqual(list.map((kept) => kept.id), ["a"]);
  });

  test("keeps the newest and lets the oldest go past the limit", () => {
    let list = [];
    for (let index = 0; index <= MAX_RECENTS; index += 1) {
      list = withSaved(list, conversation(`c${index}`));
    }
    assert.equal(list.length, MAX_RECENTS);
    assert.equal(list[0].id, `c${MAX_RECENTS}`);
    assert.ok(!list.some((kept) => kept.id === "c0"));
  });
});

describe("without", () => {
  test("removes the one conversation named", () => {
    const list = without([conversation("a"), conversation("b")], "a");
    assert.deepEqual(list.map((kept) => kept.id), ["b"]);
  });
});

describe("scopedTo", () => {
  test("the chat page and one application's dock each see their own", () => {
    const list = [conversation("a"), conversation("b", { applicationId: "app-1" })];
    assert.deepEqual(scopedTo(list, undefined).map((kept) => kept.id), ["a"]);
    assert.deepEqual(scopedTo(list, "app-1").map((kept) => kept.id), ["b"]);
    assert.deepEqual(scopedTo(list, "app-2"), []);
  });
});

describe("fitted", () => {
  test("drops the oldest until what is left fits", () => {
    const list = [conversation("new"), conversation("mid"), conversation("old")];
    const room = JSON.stringify(list.slice(0, 2)).length;
    assert.deepEqual(fitted(list, room).map((kept) => kept.id), ["new", "mid"]);
  });

  test("keeps everything that fits", () => {
    const list = [conversation("a")];
    assert.equal(fitted(list, 1_000_000), list);
  });
});

describe("parseRecents", () => {
  test("reads back what was written", () => {
    const list = [conversation("a")];
    assert.deepEqual(parseRecents(JSON.stringify(list)), list);
  });

  test("reads nothing from an empty or broken store", () => {
    assert.deepEqual(parseRecents(null), []);
    assert.deepEqual(parseRecents("{not json"), []);
    assert.deepEqual(parseRecents('{"id":"a"}'), []);
  });

  test("drops an entry that is not a conversation and keeps the rest", () => {
    const raw = JSON.stringify([
      conversation("a"),
      { id: "b", title: "no turns" },
      conversation("c", { turns: [{ role: "system", text: "x" }] }),
      conversation("d", { turns: [{ role: "you", text: 7 }] }),
      conversation("e", { updatedAt: "not a date" }),
      "junk",
    ]);
    assert.deepEqual(parseRecents(raw).map((kept) => kept.id), ["a"]);
  });
});

describe("the store", () => {
  test("a kept conversation is read back after a reload", () => {
    const store = storage();
    const outcome = keepIn(store, conversation("a"));
    assert.equal(outcome.kept, true);
    assert.deepEqual(readRecents(store).map((kept) => kept.id), ["a"]);
  });

  test("a conversation with no turns is not kept", () => {
    const store = storage();
    assert.equal(keepIn(store, conversation("a", { turns: [] })).kept, false);
    assert.equal(store.held[RECENTS_KEY], undefined);
  });

  test("a full store says so and loses nothing it held", () => {
    const before = JSON.stringify([conversation("a")]);
    const store = storage({ [RECENTS_KEY]: before }, { full: true });
    const outcome = keepIn(store, conversation("b"));
    assert.equal(outcome.kept, false);
    assert.equal(store.held[RECENTS_KEY], before);
    assert.deepEqual(outcome.list.map((kept) => kept.id), ["a"]);
  });

  test("a store that cannot be read is an empty one", () => {
    const broken = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => undefined,
    };
    assert.deepEqual(readRecents(broken), []);
  });

  test("writing reports whether it was stored", () => {
    assert.equal(writeRecents(storage(), [conversation("a")]), true);
    assert.equal(writeRecents(storage({}, { full: true }), [conversation("a")]), false);
  });
});
