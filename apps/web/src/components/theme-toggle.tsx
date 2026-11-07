"use client";

import { useEffect, useState } from "react";
import { Icon, type IconName } from "@/components/icon";

type Choice = "system" | "light" | "dark";
const KEY = "jobrunner-theme";

/** An explicit [data-theme] wins; system removes it and uses the media query. */
export function ThemeToggle() {
  const [choice, setChoice] = useState<Choice>("system");
  useEffect(() => {
    try {
      const held = localStorage.getItem(KEY);
      const next = held === "light" || held === "dark" ? held : "system";
      setChoice(next);
      apply(next);
    } catch { /* The system preference remains usable without browser storage. */ }
  }, []);
  function apply(next: Choice) {
    const root = document.documentElement;
    if (next === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", next);
  }
  function choose(next: Choice) {
    setChoice(next);
    apply(next);
    try { localStorage.setItem(KEY, next); } catch { /* Keep this session's choice. */ }
  }
  const options: { value: Choice; label: string; icon: IconName }[] = [
    { value: "light", label: "Light", icon: "sun" },
    { value: "dark", label: "Dark", icon: "moon" },
    { value: "system", label: "System", icon: "monitor" },
  ];
  return (
    <div role="group" aria-label="Theme" className="layout-switch">
      {options.map((option) => (
        <button key={option.value} type="button" onClick={() => choose(option.value)}
          aria-pressed={choice === option.value} aria-label={option.label} title={option.label}
          className="layout-choice">
          <Icon name={option.icon} />
        </button>
      ))}
    </div>
  );
}
