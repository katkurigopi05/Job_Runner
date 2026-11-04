"use client";

import { useEffect } from "react";
import { releasedName } from "@/lib/external";

/**
 * Hand a job tab's name back when the dashboard loads in it.
 *
 * A tab keeps `window.name` when it navigates. Open the dashboard inside the
 * tab a posting link opened (a typed address, a bookmark) and that tab is
 * both the dashboard and `jobrunner-form`: every later posting link loads
 * into it, and Back reloads the page from the top. Measured in Chromium on
 * 2026-10-29, for links with and without `noreferrer` alike.
 *
 * In the root layout, so it runs on every full page load, the only way a
 * named tab becomes the dashboard. Renders nothing.
 */
export function ReleaseWindowName(): null {
  useEffect(() => {
    window.name = releasedName(window.name);
  }, []);
  return null;
}
