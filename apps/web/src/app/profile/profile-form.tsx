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
      <label htmlFor={name} className="font-mono text-xs uppercase tracking-widest text-ink-soft">
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
  const [state, run] = useActionState<SaveResult | null, FormData>(action, null);

  return (
    <form action={run} className="space-y-8">
      <div className="grid gap-6 sm:grid-cols-2">
        <Field name="label" label="Label" defaultValue={profile.label} />
        <Field name="phone" label="Phone" defaultValue={profile.phone} />
        <Field name="location" label="Location" defaultValue={profile.location} />
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
          <span className="text-sm">I will need visa sponsorship</span>
        </label>
      </div>

      <fieldset className="border border-rule px-5 py-5">
        <legend className="px-2 font-mono text-xs uppercase tracking-widest text-ink-soft">
          Auto-submit
        </legend>
        <p className="max-w-prose text-sm text-ink-soft">
          Off by default, and off is the safe setting. With it on, an application scoring at or
          above your threshold is sent without stopping for you. It also requires{" "}
          <code className="font-mono text-xs">AUTO_SUBMIT=true</code> in the environment — both
          halves, deliberately.
        </p>
        <label className="mt-4 flex items-center gap-3">
          <input
            type="checkbox"
            name="auto_submit"
            defaultChecked={profile.auto_submit}
            className="size-4 accent-attn"
          />
          <span className="text-sm">Let this profile submit without asking me</span>
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
          <p role="status" className={`font-mono text-xs ${state.ok ? "text-go" : "text-stop"}`}>
            {state.message}
          </p>
        ) : null}
      </div>
    </form>
  );
}
