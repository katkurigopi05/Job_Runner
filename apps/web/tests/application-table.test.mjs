import assert from "node:assert/strict";
import { test } from "node:test";
import {
  createTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
} from "@tanstack/react-table";
import {
  applicationRows,
  applicationColumns,
  trackerColumns,
} from "../src/lib/application-table.ts";

const applications = [
  {
    id: "b",
    profile_id: "p1",
    url: "https://beta.example/jobs/1",
    status: "submitted",
    outcome: "interview",
    created_at: "2026-01-12T00:00:00Z",
    updated_at: "2026-01-14T00:00:00Z",
  },
  {
    id: "a",
    profile_id: "p1",
    url: "https://alpha.example/jobs/2",
    status: "queued",
    outcome: null,
    created_at: "2026-01-10T00:00:00Z",
    updated_at: "2026-01-15T00:00:00Z",
  },
  {
    id: "c",
    profile_id: "p2",
    url: "https://beta.example/jobs/1",
    status: "submitted",
    outcome: null,
    created_at: "2026-01-11T00:00:00Z",
    updated_at: "2026-01-13T00:00:00Z",
  },
];
const matches = [
  {
    profile_id: "p1",
    url: applications[0].url,
    title: "Cloud Engineer",
    score: 0.75,
  },
  {
    profile_id: "p1",
    url: applications[1].url,
    title: "Agent Tools Engineer",
    score: 0.25,
  },
];
const data = applicationRows(applications, matches, true);
function rows(columns, sorting = [], globalFilter = "") {
  const table = createTable({
    data,
    columns,
    state: { sorting, globalFilter },
    getRowId: (row) => row.id,
    onStateChange: () => {},
    renderFallbackValue: null,
    globalFilterFn: "includesString",
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });
  return table.getRowModel().rows.map((row) => row.id);
}

test("application rows join scores by profile and URL, preserve outcomes and never fabricate missing scores", () => {
  assert.equal(data[0].score, 0.75);
  assert.equal(data[2].score, undefined);
  assert.equal(data[2].status, "awaiting");
  assert.equal(data[0].status, "interview");
  assert.equal(applicationRows(applications)[0].status, "submitted");
  assert.equal(data[0].application, applications[0]);
});

test("application table sorts company/role, status and created time in both directions", () => {
  for (const [id, expected] of [
    ["company", ["a", "b", "c"]],
    ["status", ["c", "b", "a"]],
    ["created", ["a", "c", "b"]],
  ]) {
    assert.deepEqual(rows(applicationColumns, [{ id, desc: false }]), expected);
    if (id !== "company")
      assert.deepEqual(
        rows(applicationColumns, [{ id, desc: true }]),
        [...expected].reverse(),
      );
  }
});

test("tracker sorts numeric match scores with unknown values last and dates chronologically", () => {
  assert.deepEqual(rows(trackerColumns, [{ id: "score", desc: true }]), [
    "b",
    "a",
    "c",
  ]);
  assert.deepEqual(rows(trackerColumns, [{ id: "score", desc: false }]), [
    "a",
    "b",
    "c",
  ]);
  assert.deepEqual(rows(trackerColumns, [{ id: "updated", desc: false }]), [
    "c",
    "b",
    "a",
  ]);
});

test("text search filters case-insensitively by company, role and status while composing with sorting", () => {
  for (const columns of [applicationColumns, trackerColumns]) {
    assert.deepEqual(rows(columns, [], "ALPHA"), ["a"]);
    assert.deepEqual(rows(columns, [], "Agent Tools"), ["a"]);
    assert.deepEqual(rows(columns, [], "INTERVIEW"), ["b"]);
    assert.deepEqual(rows(columns, [{ id: "company", desc: false }], "beta"), [
      "b",
      "c",
    ]);
    assert.deepEqual(rows(columns, [], "no-such-company"), []);
    assert.deepEqual(rows(columns), ["b", "a", "c"]);
  }
});
