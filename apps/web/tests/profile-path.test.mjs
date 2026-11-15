import assert from "node:assert/strict";
import { test } from "node:test";
import { profilePath } from "../src/lib/profile-path.ts";

const ID = "12345678-abcd-1234-abcd-123456789abc";

test("profile paths accept stored UUIDs without imposing a UUID version", () => {
  for (const id of [ID, ID.toUpperCase(), "00000000-0000-0000-0000-000000000001"]) {
    assert.equal(profilePath(id), `/profiles/${id}`);
  }
});

test("profile paths reject traversal, URLs, query strings and malformed identifiers", () => {
  for (const id of ["", "../setup", "//example.com", "https://example.com", `${ID}/..`,
    `${ID}?auto_submit=true`, `${ID}#fragment`, `${ID}\n`, ID.replaceAll("-", ""), "a".repeat(100_000)]) {
    assert.equal(profilePath(id), null);
  }
});

test("profile paths reject forged server-action argument types", () => {
  for (const id of [null, undefined, 1, {}, [ID]]) assert.equal(profilePath(id), null);
});
