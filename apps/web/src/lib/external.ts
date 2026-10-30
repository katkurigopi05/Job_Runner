/**
 * Where a link that leaves the dashboard opens. Every one goes through here,
 * so all pages behave the same way (`tests/test_dashboard_job_links.py`).
 *
 * Two kinds, chosen by the owner on 2026-10-29:
 *
 * - **A posting** opens in one reused tab, `POSTING_WINDOW`. Every posting
 *   link once used `_blank`, so working through a queue of fifty left fifty
 *   tabs behind. Reuse needs the opener link: measured in Chromium, a tab
 *   opened with `noopener` or `noreferrer`, or whose opener was cut right
 *   after opening, is not found by name again, so every click opened a new
 *   tab. The cost is that a posting page can redirect the dashboard through
 *   `window.opener`. Accepted for postings, which are read rather than
 *   signed into.
 * - **An application form** opens a new tab each time, with no opener. Sign-in
 *   pages are where a site most often treats itself as a popup and redirects
 *   the window that opened it, and here that window is the dashboard.
 *
 * `DOC_WINDOW` holds the résumé PDF, which this dashboard serves itself, so
 * its opener link reaches nothing outside. It is its own tab so a posting and
 * a résumé do not evict each other.
 *
 * An ATS form cannot be embedded instead. Greenhouse, Lever, Ashby and
 * Workable all send `X-Frame-Options`/`frame-ancestors`, and a captcha would
 * refuse to run in a frame even if they did not, which is the whole reason
 * submission is the owner's step.
 */

/**
 * The value is older than the name: forms used this tab too until 2026-10-29.
 * It stays, because a dashboard tab already carrying it has to be recognised
 * (`components/release-window-name.tsx`).
 */
export const POSTING_WINDOW = "jobrunner-form";
export const DOC_WINDOW = "jobrunner-doc";

/** Every name this dashboard gives a tab. None of them may stay on a dashboard tab. */
export const WINDOW_NAMES: readonly string[] = [POSTING_WINDOW, DOC_WINDOW];

/** The name a tab showing the dashboard should carry: never one of ours. */
export function releasedName(name: string): string {
  return WINDOW_NAMES.includes(name) ? "" : name;
}

/** `<a {...POSTING_LINK}>`: the one reused posting tab. No `rel`, see above. */
export const POSTING_LINK = { target: POSTING_WINDOW } as const;

/** `<a {...APPLY_LINK}>`: a new tab for an application form, with no way back. */
export const APPLY_LINK = { target: "_blank", rel: "noopener noreferrer" } as const;

/** `APPLY_LINK` for a button. */
export function openApplyForm(url: string): void {
  window.open(url, "_blank", "noopener,noreferrer");
}

/** A résumé from this dashboard's own API, in its one reused tab. */
export function openDocument(url: string): void {
  window.open(url, DOC_WINDOW);
}
