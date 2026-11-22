// A recurring skill gap's drill-down (`src/lib/gap-evidence.ts`), on Node's own
// test runner. The answer carries every job id behind "required by N jobs"
// and the first few in full; the rest are read on request by those ids.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { parseRecents } from "../src/lib/chat-recents.ts";
import {
  DECLARATION_NOTE,
  GAP_PAGE,
  declarationOf,
  gapsOf,
  nextIds,
  pinOf,
  shownOf,
  stateOf,
} from "../src/lib/gap-evidence.ts";

const SHA = "0123456789abcdef0123456789abcdef01234567";

function gap(total, shown = Math.min(total, 10)) {
  const ids = Array.from({ length: total }, (_, n) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`);
  return {
    skill: "kubernetes",
    label: "Kubernetes",
    source_label: "P1",
    total,
    reviewed: 912,
    posting_ids: ids,
    jobs: ids.slice(0, shown).map((posting_id) => ({
      posting_id,
      title: "Platform Engineer",
      company: "Acme",
      location: "Remote, US",
      url: "https://jobs.example.test/1",
      quote: "Kubernetes is required",
      state: "as_answered",
    })),
    projects: [],
  };
}

describe("how many of N are on screen", () => {
  test("says how many are shown, and all when they are", () => {
    assert.equal(shownOf(10, 37), "Showing 10 of 37 jobs");
    assert.equal(shownOf(37, 37), "All 37 jobs shown");
    assert.equal(shownOf(1, 1), "All 1 job shown");
  });

  test("a job deleted since the answer is said, not left looking hidden", () => {
    assert.equal(shownOf(270, 283, 13), "All 283 jobs read · 13 no longer exist");
    assert.equal(shownOf(30, 283, 5), "Showing 30 of 283 jobs · 5 no longer exist");
  });

  test("the next page is the ids after those shown, from the answer's own list", () => {
    const g = gap(37);
    assert.deepEqual(nextIds(g, 10), g.posting_ids.slice(10, 10 + GAP_PAGE));
    assert.deepEqual(nextIds(g, 35), g.posting_ids.slice(35));
    assert.deepEqual(nextIds(g, 37), []);
    assert.ok(GAP_PAGE <= 50, "the API reads at most 50 at a time");
  });
});

describe("what a project's evidence is called", () => {
  const base = { project_id: "p", repository: "example/infra", url: "https://github.com/example/infra", category: "declared", commit_sha: null };

  test("a declaration is a declaration, never implementation", () => {
    assert.equal(declarationOf({ ...base, declared_in: ["dependency_file"] }), "Declared in a dependency file");
    assert.equal(
      declarationOf({ ...base, declared_in: ["primary_language", "dependency_file"] }),
      "Declared as its primary language and in a dependency file",
    );
    for (const places of [["dependency_file"], ["primary_language"]]) {
      assert.doesNotMatch(declarationOf({ ...base, declared_in: places }), /implement/i);
    }
  });

  test("says which version of the repository it was read from", () => {
    assert.equal(pinOf({ ...base, declared_in: ["dependency_file"], commit_sha: SHA }), "checked live at commit 0123456789");
    assert.match(pinOf({ ...base, declared_in: ["dependency_file"] }), /not pinned to a commit/);
  });

  test("the note keeps contribution the owner's to confirm", () => {
    assert.match(DECLARATION_NOTE, /confirm your contribution in Resume Studio/);
  });
});

describe("jobs read again", () => {
  test("say when they no longer count", () => {
    const job = gap(1).jobs[0];
    assert.equal(stateOf(job), "");
    assert.equal(stateOf({ ...job, state: "current" }), "");
    assert.equal(stateOf({ ...job, state: "closed" }), "closed since this answer");
    assert.equal(stateOf({ ...job, state: "changed" }), "no longer states this requirement");
  });
});

describe("older answers", () => {
  test("a reply or a saved turn without drill-downs has none", () => {
    assert.deepEqual(gapsOf(undefined), []);
    assert.deepEqual(gapsOf(null), []);
    assert.deepEqual(gapsOf("not a list"), []);
    assert.deepEqual(gapsOf([{ skill: "half a gap" }]), []);
    assert.equal(gapsOf([gap(3)]).length, 1);
  });

  test("a conversation saved before drill-downs still opens, and a new one keeps them", () => {
    const older = {
      id: "older",
      title: "Which skill gaps recur?",
      updatedAt: "2026-11-12T20:00:00.000Z",
      applicationId: null,
      turns: [
        { role: "you", text: "Which skill gaps recur across my strongest matches?" },
        { role: "assistant", text: "Kubernetes recurs.", provider: "ollama", retrievalMethod: "graph", retrievalDetails: "P1: …" },
      ],
    };
    const newer = { ...older, id: "newer", turns: [older.turns[0], { ...older.turns[1], gapEvidence: [gap(12)] }] };

    const [first, second] = parseRecents(JSON.stringify([older, newer]));

    assert.equal(first.turns[1].gapEvidence, undefined);
    assert.deepEqual(gapsOf(first.turns[1].gapEvidence), []);
    assert.equal(gapsOf(second.turns[1].gapEvidence)[0].posting_ids.length, 12);
  });
});
