import assert from "node:assert/strict";
import { test } from "node:test";
import { zodResolver } from "@hookform/resolvers/zod";
import {
  profileFormSchema,
  internshipSchema,
  certificationSchema,
  evidenceFormSchema,
} from "../src/lib/schemas.ts";

const internship = {
  company: " Example Labs ",
  role: "Intern",
  started: "Jun 2025",
  ended: null,
  location: null,
  stack: ["Python"],
  bullets: ["Built test tooling."],
  verified: false,
};
const certification = {
  name: "Cloud Fundamentals",
  issuer: "Example Institute",
  issued: null,
  expires: null,
  credential_id: null,
  skills: [],
  verified: false,
};

test("profile validates email, salary and search settings without requiring absent settings", () => {
  assert.equal(
    profileFormSchema.safeParse({
      email: "alex@example.com",
      min_salary: 0,
      target_roles: ["SWE"],
      target_locations: ["Remote"],
      visa_status: "Authorized",
    }).success,
    true,
  );
  for (const data of [
    { email: "invalid" },
    { min_salary: -1 },
    { min_salary: Infinity },
    { target_roles: [" "] },
    { target_locations: [""] },
    { citizenship_status: "invented" },
    { target_seniority: "invented" },
  ]) {
    assert.equal(
      profileFormSchema.safeParse(data).success,
      false,
      JSON.stringify(data),
    );
  }
  assert.deepEqual(profileFormSchema.parse({}), {});
});

test("profile keeps fractional scores and blank years distinct from zero", () => {
  for (const value of ["0", "0.25", 1, ""])
    assert.equal(
      profileFormSchema.safeParse({ min_match_score: value }).success,
      true,
    );
  for (const value of ["-1", "1.01", "NaN", Infinity])
    assert.equal(
      profileFormSchema.safeParse({ min_match_score: value }).success,
      false,
    );
  for (const value of ["0", "50", 0, ""])
    assert.equal(
      profileFormSchema.parse({ max_required_experience_years: value })
        .max_required_experience_years,
      value,
    );
  for (const value of ["-1", "51", "oops"])
    assert.equal(
      profileFormSchema.safeParse({ max_required_experience_years: value })
        .success,
      false,
    );
});

test("internships require company and role and preserve verbatim evidence and confirmation", () => {
  assert.deepEqual(internshipSchema.parse(internship), internship);
  for (const patch of [
    { company: "" },
    { company: "  " },
    { role: "" },
    { role: "\n" },
  ])
    assert.equal(
      internshipSchema.safeParse({ ...internship, ...patch }).success,
      false,
    );
});

test("certifications require a name without inventing issuer or verification", () => {
  assert.deepEqual(
    certificationSchema.parse({ ...certification, issuer: "" }),
    { ...certification, issuer: "" },
  );
  for (const name of ["", "   "])
    assert.equal(
      certificationSchema.safeParse({ ...certification, name }).success,
      false,
    );
});

test("evidence resolver returns inline array field errors and accepts removal of invalid rows", async () => {
  const resolver = zodResolver(evidenceFormSchema);
  const invalid = await resolver(
    {
      internships: [{ ...internship, company: "" }],
      certifications: [{ ...certification, name: "" }],
    },
    undefined,
    {
      criteriaMode: "firstError",
      shouldUseNativeValidation: false,
      fields: {},
    },
  );
  assert.equal(
    invalid.errors.internships[0].company.message,
    "Enter a company.",
  );
  assert.equal(
    invalid.errors.certifications[0].name.message,
    "Enter a certification name.",
  );
  const valid = await resolver(
    { internships: [internship], certifications: [] },
    undefined,
    {
      criteriaMode: "firstError",
      shouldUseNativeValidation: false,
      fields: {},
    },
  );
  assert.deepEqual(valid.errors, {});
  assert.deepEqual(valid.values.internships[0], internship);
});
