// Unit tests for the dashboard's pure helpers, on Node's own test runner.
// The files under src/lib that import nothing are plain enough for Node to
// run as they are (it strips the types), so no test framework is installed.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { countedMatches } from "../src/lib/chat-counts.ts";
import { blocksOf, cells } from "../src/lib/reply-blocks.ts";
import { deskDateTime, sentenceCase } from "../src/lib/text.ts";

describe("sentenceCase", () => {
  test("upper-cases only the first letter", () => {
    assert.equal(sentenceCase("skills"), "Skills");
    assert.equal(sentenceCase("work history"), "Work history");
  });

  test("returns an empty string unchanged", () => {
    assert.equal(sentenceCase(""), "");
  });
});

describe("deskDateTime", () => {
  test("prints the owner's zone whatever zone the machine is in", () => {
    // 06:08 UTC on Monday 9 November is 22:08 on Sunday 8 November in Los Angeles.
    assert.equal(deskDateTime("2026-11-09T06:08:00Z"), "Sunday 8 November, 22:08");
  });

  test("uses a 24-hour clock at midnight", () => {
    assert.equal(deskDateTime("2026-07-01T07:00:00Z"), "Wednesday 1 July, 00:00");
  });
});

describe("countedMatches", () => {
  test("says nothing when retrieval counted nothing", () => {
    assert.equal(countedMatches({}), null);
    assert.equal(countedMatches({ matchedTotal: null, matchedRole: "data engineer" }), null);
  });

  test("names the role, with a thousands separator and a plural", () => {
    const label = countedMatches({ matchedTotal: 1234, matchedRole: "data engineer" });

    assert.equal(label, "1,234 open data engineer postings in your search area");
  });

  test("uses the singular for exactly one posting", () => {
    const label = countedMatches({ matchedTotal: 1, matchedRole: "data engineer" });

    assert.equal(label, "1 open data engineer posting in your search area");
  });

  test("lists title words, then the related postings without them", () => {
    const label = countedMatches({ matchedTotal: 12, titleWords: ["senior", "python"], relatedTotal: 40 });

    assert.equal(
      label,
      "12 open postings with senior and python in the title, in your search area, then 40 related without it",
    );
  });

  test("warns that a mention is not a requirement", () => {
    const label = countedMatches({ matchedTotal: 30, matchedTerm: "Kafka", companiesTotal: 7 });

    assert.equal(
      label,
      "30 open postings at 7 companies mentioning Kafka in your search area; mentions do not necessarily mean required skills",
    );
  });

  test("names the companies a question asked about", () => {
    const label = countedMatches({ matchedTotal: 5, matchedCompanies: ["Stripe", "Figma"] });

    assert.equal(label, "5 open postings at Stripe or Figma in your search area");
  });

  test("says nothing for a total with no role, word, term or company", () => {
    assert.equal(countedMatches({ matchedTotal: 5 }), null);
  });
});

describe("cells", () => {
  test("splits a pipe row and trims each cell", () => {
    assert.deepEqual(cells("| Company | Open roles |"), ["Company", "Open roles"]);
  });
});

describe("blocksOf", () => {
  test("keeps the lines of a paragraph together", () => {
    assert.deepEqual(blocksOf("First line\nsecond line"), [
      { kind: "paragraph", lines: ["First line", "second line"] },
    ]);
  });

  test("reads bullets and numbered items as separate lists", () => {
    const blocks = blocksOf("- one\n- two\n\n1. first\n2) second");

    assert.deepEqual(blocks, [
      { kind: "bullets", items: ["one", "two"] },
      { kind: "numbered", items: ["first", "second"] },
    ]);
  });

  test("takes the row above a rule as the table's header", () => {
    const blocks = blocksOf("| Company | Roles |\n|---|---|\n| Acme | 3 |");

    assert.deepEqual(blocks, [{ kind: "table", header: ["Company", "Roles"], rows: [["Acme", "3"]] }]);
  });

  test("reads a table without a rule as rows only", () => {
    const blocks = blocksOf("| Acme | 3 |\n| Globex | 5 |");

    assert.deepEqual(blocks, [{ kind: "table", header: null, rows: [["Acme", "3"], ["Globex", "5"]] }]);
  });

  test("ends a paragraph where a list begins", () => {
    const blocks = blocksOf("Three boards:\n- Acme\n- Globex");

    assert.deepEqual(blocks, [
      { kind: "paragraph", lines: ["Three boards:"] },
      { kind: "bullets", items: ["Acme", "Globex"] },
    ]);
  });

  test("returns nothing for blank text", () => {
    assert.deepEqual(blocksOf("\n  \n"), []);
  });
});
