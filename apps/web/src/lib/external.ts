/**
 * Open something outside the dashboard, in ONE reused tab.
 *
 * Every posting link, every apply form, every résumé used `_blank`, so working
 * through a queue of fifty left fifty tabs behind. A *named* target reuses the
 * same window instead: the second posting replaces the first, and the browser
 * never accumulates.
 *
 * Two windows rather than one, because they are used together and would
 * otherwise evict each other — the form stays open while the résumé is
 * fetched.
 */
export const FORM_WINDOW = "jobrunner-form";
export const DOC_WINDOW = "jobrunner-doc";

/**
 * An ATS form cannot be embedded. Greenhouse, Lever, Ashby and Workable all
 * send `X-Frame-Options`/`frame-ancestors`, and a captcha would refuse to run
 * in a frame even if they did not — which is the whole reason submission is
 * the owner's step. So this is a real window, deliberately, and the only
 * question left is whether it is a *new* one each time. It is not.
 */
export function openExternal(url: string, target: string = FORM_WINDOW): void {
  // Deliberately WITHOUT `noopener`. Per the HTML spec `noopener` forces a
  // fresh browsing context, which would ignore the name and open a new tab
  // every time — exactly the behaviour this replaces. `noreferrer` implies
  // `noopener`, so it is out for the same reason.
  //
  // The cost is that the opened page can see `window.opener`. Accepted here
  // and worth stating: the dashboard is localhost-only, refuses non-loopback
  // callers, and holds no session a third party could use — while the
  // alternative is fifty abandoned tabs after a morning's queue. Reverse
  // tabnabbing would let an ATS page navigate this one, which is visible and
  // recoverable; the tab sprawl is neither.
  window.open(url, target);
}
