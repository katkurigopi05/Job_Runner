"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import type { Profile } from "@/lib/api";
import { saveProfile, type SaveResult } from "./actions";

function Field({
  name,
  label,
  hint,
  defaultValue,
  type = "text",
}: {
  name: string;
  label: string;
  hint?: string;
  defaultValue?: string | null;
  type?: string;
}) {
  return (
    <div>
      <label
        htmlFor={name}
        className="font-mono text-xs uppercase tracking-widest text-ink-soft"
      >
        {label}
      </label>
      {hint ? <p className="mt-1 text-sm text-ink-faint">{hint}</p> : null}
      <input
        id={name}
        name={name}
        type={type}
        defaultValue={defaultValue ?? ""}
        className="mt-2 w-full rounded-md border border-rule bg-paper px-3 py-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
      />
    </div>
  );
}

function Save() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="rounded-md bg-ink px-6 py-2.5 font-mono text-xs uppercase tracking-widest text-paper transition-opacity hover:opacity-85 disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
    >
      {pending ? "saving…" : "save"}
    </button>
  );
}

export function ProfileForm({ profile }: { profile: Profile }) {
  const action = saveProfile.bind(null, profile.id);
  const [state, run] = useActionState<SaveResult | null, FormData>(
    action,
    null,
  );

  return (
    <form action={run} className="space-y-8">
      <div className="grid gap-6 sm:grid-cols-2">
        <Field name="label" label="Label" defaultValue={profile.label} />
        <Field name="phone" label="Phone" defaultValue={profile.phone} />
        <Field
          name="location"
          label="Location"
          defaultValue={profile.location}
        />
        <Field
          name="salary_expectation"
          label="Salary expectation"
          defaultValue={profile.salary_expectation}
        />
      </div>

      <div className="border-l-2 border-attn pl-5">
        <Field
          name="work_auth"
          label="Work authorization"
          hint="Copied onto applications word for word. Never generated, never paraphrased — write it exactly as you would on a form."
          defaultValue={profile.work_auth}
        />
        <label className="mt-4 flex items-center gap-3">
          <input
            type="checkbox"
            name="needs_sponsorship"
            defaultChecked={profile.needs_sponsorship ?? false}
            className="size-4 accent-attn"
          />
          <span className="text-sm">
            I will need visa sponsorship in future
          </span>
        </label>

        {/* A separate question from the one above, and the reason the column
            exists: a permanent resident needs no sponsorship and still fails
            "US citizens only". Unstated is the shipped state and never
            excludes a posting — an explicit restriction is surfaced on the
            match card instead of acted on. */}
        <div className="mt-5">
          <label
            htmlFor={`citizenship-${profile.id}`}
            className="font-mono text-xs uppercase tracking-widest text-ink-soft"
          >
            Current status, for filtering only
          </label>
          <p className="mt-1 max-w-prose text-sm text-ink-soft">
            Never typed onto an application — the box above is what gets copied.
            This only decides which postings you are shown: leave it unset and
            nothing is filtered out.
          </p>
          <select
            id={`citizenship-${profile.id}`}
            name="citizenship_status"
            defaultValue={profile.citizenship_status ?? ""}
            className="mt-2 w-full max-w-sm rounded-md border border-rule bg-paper px-3 py-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
          >
            <option value="">Not stated — filter nothing</option>
            <option value="us_citizen">US citizen</option>
            <option value="permanent_resident">
              Permanent resident / green card
            </option>
            <option value="other_authorized">
              Otherwise authorized to work now
            </option>
            <option value="not_authorized">Not currently authorized</option>
          </select>
        </div>
      </div>

      {/* Search filters, kept apart from everything above on purpose.
          CLAUDE.md §1: what the owner wants to *see* and what goes on their
          application are different things, and conflating them means
          narrowing a search also changes what gets typed into a form.

          Neither control existed before. `target_seniority` shipped with a
          measured payoff — P@10 0.900 to 1.000 on the Gate 5 set — and no way
          for the owner to set it outside curl, which is the same defect §15
          records for `citizenship_status`: a filter with no control can never
          fire. */}
      <fieldset className="border border-rule px-5 py-5">
        <legend className="px-2 font-mono text-xs uppercase tracking-widest text-ink-soft">
          Which postings you see
        </legend>
        <p className="max-w-prose text-sm text-ink-soft">
          These narrow the feed and nothing else. Your answers above — the part
          copied onto a real application — are untouched by them. Leave either
          unset to filter nothing.
        </p>

        <div className="mt-5 flex flex-wrap gap-8">
          <div>
            <label
              htmlFor={`seniority-${profile.id}`}
              className="font-mono text-xs uppercase tracking-widest text-ink-soft"
            >
              Applying at
            </label>
            <select
              id={`seniority-${profile.id}`}
              name="target_seniority"
              defaultValue={profile.target_seniority ?? ""}
              className="mt-2 block w-56 rounded-md border border-rule bg-paper px-3 py-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
            >
              <option value="">Any level</option>
              <option value="intern">intern / apprentice</option>
              <option value="junior">junior</option>
              <option value="mid">mid</option>
              <option value="senior">senior</option>
              <option value="staff">staff</option>
              <option value="principal">principal</option>
            </select>
            <p className="mt-2 max-w-xs text-xs text-ink-faint">
              One rung either side is still shown.
            </p>
          </div>

          <div>
            <label
              htmlFor={`max-years-${profile.id}`}
              className="font-mono text-xs uppercase tracking-widest text-ink-soft"
            >
              Most years a posting may ask for
            </label>
            <input
              id={`max-years-${profile.id}`}
              name="max_required_experience_years"
              type="number"
              min={0}
              max={50}
              step={1}
              placeholder="no limit"
              defaultValue={
                profile.max_required_experience_years === null
                  ? ""
                  : String(profile.max_required_experience_years)
              }
              className="mt-2 block w-56 rounded-md border border-rule bg-paper px-3 py-2 tabular-nums focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn"
            />
            <p className="mt-2 max-w-xs text-xs text-ink-faint">
              Your bound, not your experience. A posting that says the years are
              preferred rather than required is kept either way — it is marked
              on the card instead.
            </p>
          </div>
        </div>
      </fieldset>

      <fieldset className="border border-rule px-5 py-5">
        <legend className="px-2 font-mono text-xs uppercase tracking-widest text-ink-soft">
          Auto-submit
        </legend>
        <p className="max-w-prose text-sm text-ink-soft">
          Off by default, and off is the safe setting. With it on, an
          application scoring at or above your threshold is sent without
          stopping for you. It also requires{" "}
          <code className="font-mono text-xs">AUTO_SUBMIT=true</code> in the
          environment — both halves, deliberately.
        </p>
        <label className="mt-4 flex items-center gap-3">
          <input
            type="checkbox"
            name="auto_submit"
            defaultChecked={profile.auto_submit}
            className="size-4 accent-attn"
          />
          <span className="text-sm">
            Let this profile submit without asking me
          </span>
        </label>
        <div className="mt-5 max-w-xs">
          <Field
            name="min_match_score"
            label="Minimum match score"
            hint="0 to 1. Nothing below this is ever auto-submitted."
            type="number"
            defaultValue={String(profile.min_match_score)}
          />
        </div>
      </fieldset>

      <div className="flex items-center gap-4">
        <Save />
        {state ? (
          <p
            role="status"
            className={`font-mono text-xs ${state.ok ? "text-go" : "text-stop"}`}
          >
            {state.message}
          </p>
        ) : null}
      </div>
    </form>
  );
}
