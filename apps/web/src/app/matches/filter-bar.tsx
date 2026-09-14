"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useState, useTransition } from "react";
import { saveDefaultSearch } from "./search-actions";

/* Filter state lives in the URL, not in component state. A search worth
   running twice is worth being able to bookmark, and the back button should
   undo a filter rather than leave the page. */

/**
 * The value is the API's rung name; the label is what that rung actually
 * covers. "intern" also matches apprenticeships, co-ops and traineeships —
 * one tier, several names an employer might use for it.
 */
const SENIORITY: Array<{ value: string; label: string }> = [
  { value: "intern", label: "intern / apprentice" },
  { value: "junior", label: "junior" },
  { value: "mid", label: "mid" },
  { value: "senior", label: "senior" },
  { value: "staff", label: "staff" },
  { value: "principal", label: "principal" },
];

const EDUCATION: Array<{ value: string; label: string }> = [
  { value: "high_school", label: "high school" },
  { value: "associate", label: "associate" },
  { value: "bachelor", label: "bachelor's" },
  { value: "master", label: "master's" },
  { value: "phd", label: "PhD" },
];

const CURRENCIES = ["USD", "CAD", "GBP", "EUR", "INR", "AUD"];
const PERIODS = ["year", "month", "hour"];

const FIELD =
  "rounded-[var(--radius)] border border-rule bg-paper px-3 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn";
const LABEL = "font-mono text-xs uppercase tracking-widest text-ink-soft";

export function FilterBar({
  resultCount,
  savedFilters = null,
}: {
  resultCount: number;
  savedFilters?: Record<string, string> | null;
}) {
  const router = useRouter();
  const params = useSearchParams();
  const [saving, startSaving] = useTransition();
  const [saveNote, setSaveNote] = useState<string | null>(null);

  const replace = useCallback(
    (next: URLSearchParams) =>
      router.replace(next.toString() ? `/matches?${next}` : "/matches", { scroll: false }),
    [router],
  );

  const set = useCallback(
    (key: string, value: string) => {
      const next = new URLSearchParams(params.toString());
      if (value) next.set(key, value);
      else next.delete(key);
      replace(next);
    },
    [params, replace],
  );

  const value = (key: string) => params.get(key) ?? "";
  const active = Array.from(params.keys()).length > 0;

  const text = (key: string, placeholder: string, width: string) => (
    <input
      defaultValue={value(key)}
      onBlur={(event) => set(key, event.target.value.trim())}
      onKeyDown={(event) => {
        if (event.key === "Enter") set(key, event.currentTarget.value.trim());
      }}
      placeholder={placeholder}
      className={`${width} ${FIELD}`}
    />
  );

  const unknownSwitch = (key: string, label: string) => (
    <label className="inline-flex items-center gap-2 text-xs text-ink-soft">
      <input
        type="checkbox"
        checked={value(key) === "true"}
        onChange={(event) => set(key, event.target.checked ? "true" : "")}
        className="h-3.5 w-3.5 accent-[var(--color-attn)]"
      />
      {label}
    </label>
  );

  function save() {
    setSaveNote(null);
    startSaving(async () => {
      const outcome = await saveDefaultSearch(Object.fromEntries(params.entries()));
      setSaveNote(outcome.ok ? "saved as your default search" : outcome.message);
    });
  }

  const unknownSwitches = [
    value("min_salary") ? unknownSwitch("include_unknown_salary", "include postings that don't state pay") : null,
    value("min_salary") && (value("salary_period") || "year") === "year"
      ? unknownSwitch(
          "salary_unstated_period_as_year",
          "read salaries of 10,000+ with no stated period as annual",
        )
      : null,
    value("lacking_skills")
      ? unknownSwitch("include_unknown_skills", "include postings that name a skill without saying it's required")
      : null,
    value("max_education")
      ? unknownSwitch("include_unknown_education", "include postings that don't state education")
      : null,
  ].filter(Boolean);

  return (
    <div className="rounded-[var(--radius-lg)] border border-rule bg-paper-raised p-4">
      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1">
          <span className={LABEL}>keywords</span>
          {text("keywords", "python, postgres", "w-48")}
        </label>

        <label className="flex flex-col gap-1">
          <span className={LABEL}>location</span>
          {text("locations", "austin, san jose", "w-44")}
        </label>

        <label className="flex flex-col gap-1">
          <span className={LABEL}>remote</span>
          <select value={value("remote")} onChange={(event) => set("remote", event.target.value)} className={FIELD}>
            <option value="">either</option>
            <option value="true">remote only</option>
            <option value="false">on-site only</option>
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className={LABEL}>at least</span>
          <select
            value={value("min_seniority")}
            onChange={(event) => set("min_seniority", event.target.value)}
            className={FIELD}
          >
            <option value="">any level</option>
            {SENIORITY.map((level) => (
              <option key={level.value} value={level.value}>
                {level.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className={LABEL}>seen within</span>
          <select
            value={value("posted_within_days")}
            onChange={(event) => set("posted_within_days", event.target.value)}
            className={FIELD}
          >
            <option value="">any time</option>
            <option value="1">24 hours</option>
            <option value="7">7 days</option>
            <option value="30">30 days</option>
          </select>
        </label>

        <span className="ml-auto font-mono text-xs tabular-nums text-ink-faint">{resultCount} shown</span>
      </div>

      <fieldset className="mt-4 border-t border-rule-soft pt-4">
        <legend className="sr-only">Pay, skills and education</legend>
        <div className="flex flex-wrap items-end gap-4">
          <div className="flex flex-col gap-1">
            <span className={LABEL}>pay reaching</span>
            <div className="flex gap-1">
              <input
                type="number"
                inputMode="numeric"
                min={0}
                step={1000}
                aria-label="minimum pay"
                defaultValue={value("min_salary")}
                onBlur={(event) => set("min_salary", event.target.value.trim())}
                onKeyDown={(event) => {
                  if (event.key === "Enter") set("min_salary", event.currentTarget.value.trim());
                }}
                placeholder="150000"
                className={`w-28 ${FIELD}`}
              />
              <select
                aria-label="currency"
                value={value("salary_currency") || "USD"}
                onChange={(event) => set("salary_currency", event.target.value === "USD" ? "" : event.target.value)}
                className={FIELD}
              >
                {CURRENCIES.map((code) => (
                  <option key={code}>{code}</option>
                ))}
              </select>
              <select
                aria-label="pay period"
                value={value("salary_period") || "year"}
                onChange={(event) => set("salary_period", event.target.value === "year" ? "" : event.target.value)}
                className={FIELD}
              >
                {PERIODS.map((period) => (
                  <option key={period} value={period}>
                    per {period}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <label className="flex flex-col gap-1">
            <span className={LABEL}>skills I lack</span>
            {text("lacking_skills", "java, kubernetes", "w-44")}
          </label>

          <label className="flex flex-col gap-1">
            <span className={LABEL}>must name</span>
            {text("wanted_skills", "python", "w-36")}
          </label>

          <label className="flex flex-col gap-1">
            <span className={LABEL}>my education</span>
            <select
              value={value("max_education")}
              onChange={(event) => set("max_education", event.target.value)}
              className={FIELD}
            >
              <option value="">don&apos;t filter</option>
              {EDUCATION.map((level) => (
                <option key={level.value} value={level.value}>
                  {level.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        {unknownSwitches.length > 0 ? (
          <div className="mt-3 flex flex-col gap-1.5">
            <p className="text-xs text-ink-faint">
              A posting that doesn&apos;t say is not counted as meeting these. Widen one:
            </p>
            {unknownSwitches}
          </div>
        ) : null}
      </fieldset>

      <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-rule-soft pt-3">
        {active ? (
          <>
            <button
              type="button"
              onClick={save}
              disabled={saving}
              className="rounded-[var(--radius)] border border-rule px-3 py-1.5 text-xs text-ink transition-colors hover:border-go hover:text-go disabled:opacity-50"
            >
              {saving ? "saving…" : "save as default search"}
            </button>
            <button
              type="button"
              onClick={() => router.replace("/matches", { scroll: false })}
              className="rounded-[var(--radius)] border border-rule px-3 py-1.5 text-xs text-ink-soft transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn"
            >
              clear
            </button>
          </>
        ) : savedFilters && Object.keys(savedFilters).length > 0 ? (
          <button
            type="button"
            onClick={() => replace(new URLSearchParams(savedFilters))}
            className="rounded-[var(--radius)] border border-go px-3 py-1.5 text-xs text-go transition-colors hover:bg-go hover:text-paper"
          >
            use my saved search
          </button>
        ) : null}
        {saveNote ? (
          <span role="status" className="text-xs text-ink-soft">
            {saveNote}
          </span>
        ) : null}
        <p className="ml-auto max-w-prose text-xs text-ink-faint">
          These change what you see here. Your profile — the part that gets typed into forms — stays
          exactly as it is.
        </p>
      </div>
    </div>
  );
}
