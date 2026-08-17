"use client";

import { useEffect, useState } from "react";

type Choice = "system" | "light" | "dark";

const KEY = "jobrunner-theme";

/**
 * Dark / light / system, the photo-editor's mechanism.
 *
 * An explicit choice sets [data-theme] on the root, which wins over the media
 * query. "system" removes the attribute and lets prefers-color-scheme decide.
 */
export function ThemeToggle() {
  const [choice, setChoice] = useState<Choice>("system");

  useEffect(() => {
    const held = (localStorage.getItem(KEY) as Choice | null) ?? "system";
    setChoice(held);
    apply(held);
  }, []);

  function apply(next: Choice) {
    const root = document.documentElement;
    if (next === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", next);
  }

  function choose(next: Choice) {
    setChoice(next);
    localStorage.setItem(KEY, next);
    apply(next);
  }

  const options: { value: Choice; label: string }[] = [
    { value: "light", label: "☀" },
    { value: "dark", label: "☾" },
    { value: "system", label: "auto" },
  ];

  return (
    <div
      role="group"
      aria-label="Theme"
      className="flex items-center gap-0.5 rounded-[var(--radius)] border border-rule p-0.5"
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => choose(option.value)}
          aria-pressed={choice === option.value}
          className={`rounded-[calc(var(--radius)-3px)] px-2 py-0.5 font-mono text-xs transition-colors ${
            choice === option.value
              ? "bg-paper-high text-ink"
              : "text-ink-faint hover:text-ink-soft"
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
