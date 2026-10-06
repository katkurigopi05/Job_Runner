"use client";

import { useSyncExternalStore } from "react";

export type Theme = "dark" | "light";

const LIGHT = "(prefers-color-scheme: light)";
const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";

function watchTheme(changed: () => void) {
  const system = window.matchMedia(LIGHT);
  const chosen = new MutationObserver(changed);
  chosen.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  system.addEventListener("change", changed);
  return () => {
    chosen.disconnect();
    system.removeEventListener("change", changed);
  };
}

function readTheme(): Theme {
  const chosen = document.documentElement.getAttribute("data-theme");
  if (chosen === "light" || chosen === "dark") return chosen;
  return window.matchMedia(LIGHT).matches ? "light" : "dark";
}

/**
 * The theme on screen, by the rule `globals.css` applies: an explicit
 * `[data-theme]` wins, otherwise the system preference, and dark is the base.
 *
 * For a component that cannot read the CSS variables and has to be told.
 * `border-beam`'s own `theme="auto"` looks at the system preference only, so
 * with "light" picked in `ThemeToggle` on a dark system it draws for dark.
 */
export function useResolvedTheme(): Theme {
  return useSyncExternalStore(watchTheme, readTheme, () => "dark");
}

function watchMotion(changed: () => void) {
  const query = window.matchMedia(REDUCED_MOTION);
  query.addEventListener("change", changed);
  return () => query.removeEventListener("change", changed);
}

/**
 * Whether the owner asked the system for less motion.
 *
 * True on the server: nothing should move before the answer is known, and
 * starting still then fading in is the cheaper mistake.
 */
export function usePrefersReducedMotion(): boolean {
  return useSyncExternalStore(
    watchMotion,
    () => window.matchMedia(REDUCED_MOTION).matches,
    () => true,
  );
}
