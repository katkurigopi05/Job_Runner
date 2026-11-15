"use client";

import { startTransition, useActionState } from "react";
import { zodResolver } from "@hookform/resolvers/zod";
import { FormProvider, useForm, useFormContext } from "react-hook-form";
import { profileFormSchema, type ProfileFormValues } from "@/lib/schemas";
import type { Profile } from "@/lib/api";
import { saveProfile, type SaveResult } from "./actions";

function Field({
  name,
  label,
  hint,
  type = "text",
  min,
  max,
  step,
}: {
  name: keyof ProfileFormValues;
  label: string;
  hint?: string;
  type?: string;
  min?: number;
  max?: number;
  step?: number;
}) {
  const {
    register,
    formState: { errors },
  } = useFormContext<ProfileFormValues>();
  const error = errors[name]?.message;
  return (
    <div>
      <label htmlFor={name} className="text-label text-ink-soft">
        {label}
      </label>
      {hint ? <p className="mt-1 text-sm text-ink-faint">{hint}</p> : null}
      <input
        id={name}
        {...register(name)}
        aria-invalid={!!error}
        aria-describedby={error ? `${name}-error` : undefined}
        type={type}
        min={min}
        max={max}
        step={step}
        className="mt-2 w-full rounded-lg border border-rule bg-paper px-3 py-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ink-soft"
      />
      {error ? (
        <p id={`${name}-error`} role="alert" className="mt-1 text-sm text-stop">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function Save({ pending }: { pending: boolean }) {
  return (
    <button
      type="submit"
      disabled={pending}
      className="rounded-lg bg-ink px-6 py-2 text-label text-paper transition-opacity hover:opacity-85 disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft"
    >
      {pending ? "saving…" : "save"}
    </button>
  );
}

export function ProfileForm({ profile }: { profile: Profile }) {
  const action = saveProfile.bind(null, profile.id);
  const [state, run, pending] = useActionState<SaveResult | null, FormData>(
    action,
    null,
  );

  const form = useForm<ProfileFormValues>({
    resolver: zodResolver(profileFormSchema),
    defaultValues: {
      label: profile.label,
      phone: profile.phone ?? "",
      location: profile.location ?? "",
      salary_expectation: profile.salary_expectation ?? "",
      work_auth: profile.work_auth ?? "",
      needs_sponsorship: profile.needs_sponsorship ?? false,
      auto_submit: profile.auto_submit,
      citizenship_status: profile.citizenship_status ?? "",
      target_seniority: (profile.target_seniority ??
        "") as ProfileFormValues["target_seniority"],
      min_match_score: String(profile.min_match_score),
      max_required_experience_years:
        profile.max_required_experience_years === null
          ? ""
          : String(profile.max_required_experience_years),
    },
  });

  return (
    <FormProvider {...form}>
      <form
        action={run}
        onSubmit={(event) => {
          const data = new FormData(event.currentTarget);
          void form.handleSubmit(() => startTransition(() => run(data)))(event);
        }}
        className="space-y-8"
      >
        <div className="grid gap-6 sm:grid-cols-2">
          <Field name="label" label="Label" />
          <Field name="phone" label="Phone" />
          <Field name="location" label="Location" />
          <Field name="salary_expectation" label="Salary expectation" />
        </div>

        <div className="border-l-2 border-attn pl-5">
          <Field
            name="work_auth"
            label="Work authorization"
            hint="Copied onto applications word for word. Never generated, never paraphrased — write it exactly as you would on a form."
          />
          <label className="mt-4 flex items-center gap-3">
            <input
              type="checkbox"
              {...form.register("needs_sponsorship")}
              className="size-4 accent-ink"
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
              className="text-label text-ink-soft"
            >
              Current status, for filtering only
            </label>
            <p className="mt-1 max-w-prose text-sm text-ink-soft">
              Never typed onto an application — the box above is what gets
              copied. This only decides which postings you are shown: leave it
              unset and nothing is filtered out.
            </p>
            <select
              id={`citizenship-${profile.id}`}
              {...form.register("citizenship_status")}
              className="mt-2 w-full max-w-sm rounded-lg border border-rule bg-paper px-3 py-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ink-soft"
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
          <legend className="px-2 text-label text-ink-soft">
            Which postings you see
          </legend>
          <p className="max-w-prose text-sm text-ink-soft">
            These narrow the feed and nothing else. Your answers above — the
            part copied onto a real application — are untouched by them. Leave
            either unset to filter nothing.
          </p>

          <div className="mt-5 flex flex-wrap gap-8">
            <div>
              <label
                htmlFor={`seniority-${profile.id}`}
                className="text-label text-ink-soft"
              >
                Applying at
              </label>
              <select
                id={`seniority-${profile.id}`}
                {...form.register("target_seniority")}
                className="mt-2 block w-56 rounded-lg border border-rule bg-paper px-3 py-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ink-soft"
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
                className="text-label text-ink-soft"
              >
                Most years a posting may ask for
              </label>
              <input
                id={`max-years-${profile.id}`}
                {...form.register("max_required_experience_years")}
                aria-invalid={
                  !!form.formState.errors.max_required_experience_years
                }
                aria-describedby={
                  form.formState.errors.max_required_experience_years
                    ? "max-years-error"
                    : undefined
                }
                type="number"
                min={0}
                max={50}
                step={1}
                placeholder="no limit"
                className="mt-2 block w-56 rounded-lg border border-rule bg-paper px-3 py-2 tabular-nums focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ink-soft"
              />
              {form.formState.errors.max_required_experience_years ? (
                <p
                  id="max-years-error"
                  role="alert"
                  className="text-sm text-stop"
                >
                  {form.formState.errors.max_required_experience_years.message}
                </p>
              ) : null}
              <p className="mt-2 max-w-xs text-xs text-ink-faint">
                Your bound, not your experience. A posting that says the years
                are preferred rather than required is kept either way — it is
                marked on the card instead.
              </p>
            </div>
          </div>
        </fieldset>

        <fieldset className="border border-rule px-5 py-5">
          <legend className="px-2 text-label text-ink-soft">Auto-submit</legend>
          <p className="max-w-prose text-sm text-ink-soft">
            Off by default, and off is the safe setting. With it on, an
            application scoring at or above your threshold is sent without
            stopping for you. It also requires{" "}
            <code className="text-xs">AUTO_SUBMIT=true</code> in the environment
            — both halves, deliberately.
          </p>
          <label className="mt-4 flex items-center gap-3">
            <input
              type="checkbox"
              {...form.register("auto_submit")}
              className="size-4 accent-ink"
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
              min={0}
              max={1}
              step={0.01}
            />
          </div>
        </fieldset>

        <div className="flex items-center gap-4">
          <Save pending={pending || form.formState.isSubmitting} />
          {state ? (
            <p
              role={state.ok ? "status" : "alert"}
              className={`text-xs ${state.ok ? "text-go" : "text-stop"}`}
            >
              {state.message}
            </p>
          ) : null}
        </div>
      </form>
    </FormProvider>
  );
}
