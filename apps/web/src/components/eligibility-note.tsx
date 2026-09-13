import type { Eligibility } from "@/lib/api";

/**
 * What the posting says about work authorization — and, more often, that it
 * said nothing.
 *
 * The unknown case is the one this component exists for. A card that shows an
 * authorization line only when a restriction was found teaches the reader that
 * a missing line means "fine" — so a posting nobody has checked looks
 * identical to one that explicitly sponsors. It is rendered every time, and
 * when nothing was established it says so in words: "Unknown — verify with
 * employer".
 *
 * Explicit statements carry the posting's own sentence. A restriction asserted
 * without its wording is unauditable, and this one decides whether the owner
 * spends an hour on an application they cannot accept.
 *
 * Nothing here claims OPT or STEM-OPT acceptance, E-Verify participation, or
 * past sponsorship. None of that is in the data, because none of it follows
 * from a posting not mentioning visas.
 */
export function EligibilityNote({ eligibility }: { eligibility: Eligibility }) {
  const restricted =
    eligibility.citizenship !== "unstated" || eligibility.sponsorship === "unavailable";
  // A restriction is the owner's problem to act on; anything else is a remark.
  const tone = restricted ? "aside-stop text-stop" : "text-ink-soft";

  return (
    <div className={`aside mt-4 ${tone}`}>
      <p className="text-xs">
        <span className="text-ink-faint">Work authorization</span>{" "}
        <span className={restricted ? "font-medium" : ""}>{eligibility.summary}</span>
      </p>
      {eligibility.evidence.length > 0 ? (
        <ul className="mt-1.5 space-y-1">
          {eligibility.evidence.map((item) => (
            <li key={`${item.claim}:${item.quote}`} className="text-xs text-ink-soft">
              <q className="italic">{item.quote}</q>
            </li>
          ))}
        </ul>
      ) : null}
      {!eligibility.certain ? (
        <p className="mt-1 max-w-prose text-xs text-ink-faint">
          The posting does not say. Nothing is assumed either way — ask the employer before
          counting on it.
        </p>
      ) : null}
    </div>
  );
}
