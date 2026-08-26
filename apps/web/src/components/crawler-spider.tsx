"use client";

import { useEffect, useState } from "react";
import type { CrawlStatus } from "@/lib/api";

/** Slow: this is a local queue, and a crawl runs for minutes, not milliseconds. */
const POLL_MS = 10_000;

/**
 * Whether the crawler is working, shown as a spider that moves while it is.
 *
 * The queue was invisible from every screen. `make crawl` enqueues and
 * `make worker` drains, and nothing said whether either was happening — so a
 * match feed six days stale looked exactly like a fresh one with nothing new.
 * That is how an empty "posted in the last day" search reads as "the market is
 * quiet" instead of "nothing has been looked for".
 *
 * Three states, because each needs a different thing from the owner:
 *
 * - **working** — a crawl is claimed and running. The spider walks.
 * - **waiting** — queued with nobody holding it. The spider is still and amber:
 *   `make worker` is not up, and this would otherwise look identical to working.
 * - **idle** — nothing queued. The spider sleeps: a slow breath and three z's
 *   drifting up. Nothing is crawling and nothing is wrong, and a motionless
 *   spider on its own reads the same as a broken one. The label carries the age
 *   of the newest posting, which is the number that actually answers "are my
 *   results current".
 *
 * Only genuinely idle sleeps. `queued` is waiting on a worker — a thing to fix,
 * not a thing to rest through — so it stays awake and amber.
 */
function ageInWords(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;

  const hours = Math.floor((Date.now() - then) / 3_600_000);
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function CrawlerSpider() {
  const [status, setStatus] = useState<CrawlStatus | null>(null);
  const [reachable, setReachable] = useState(true);

  useEffect(() => {
    let live = true;

    const check = async () => {
      try {
        const res = await fetch("/api/crawl/status", { cache: "no-store" });
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as CrawlStatus;
        if (live) {
          setStatus(body);
          setReachable(true);
        }
      } catch {
        // The API being down is already reported by the health pill beside
        // this one. Saying it twice would be noise, so this just goes quiet.
        if (live) setReachable(false);
      }
    };

    void check();
    const timer = setInterval(check, POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, []);

  if (!reachable || status === null) return null;

  const freshness = ageInWords(status.newest_posting_at);

  const { label, tone, moving, asleep, title } = status.running
    ? {
        label: "crawling",
        tone: "text-go",
        moving: true,
        asleep: false,
        title: "A crawl is running. New postings will appear in /matches when it finishes.",
      }
    : status.stalled
      ? {
          label: `${status.pending} queued`,
          tone: "text-attn",
          // Waiting, not resting. A crawl that nobody is draining is a problem
          // to fix, and drawing it asleep would make it look like a state the
          // owner chose.
          moving: false,
          asleep: false,
          title:
            "A crawl is queued but no worker is draining it. Start one with: make worker",
        }
      : {
          label: freshness ? `postings ${freshness}` : "idle",
          tone: "text-ink-faint",
          moving: false,
          asleep: true,
          title: freshness
            ? `Nothing is crawling. The newest posting was first seen ${freshness}. Queue a crawl with: make crawl`
            : "Nothing is crawling and no postings have been seen yet.",
        };

  return (
    <>
      {/* While a crawl runs, the spider leaves the header and walks the top
          edge of the page, left to right, on a thread. It is the only moving
          thing on screen, which is the point: the question it answers — is
          anything happening — should be answerable without reading a word.

          Drawn rather than set as the 🕷 emoji, because an emoji cannot move
          its legs and cannot be turned to face where it is going. Here the
          cephalothorax and eyes are on the right, so it looks along its own
          direction of travel, and the eight legs step in two alternating sets
          the way a real spider's do.

          Fixed and `pointer-events-none` so it crosses over the page without
          intercepting a click on whatever it passes. `translateX` and `rotate`
          only, per the project's animation rules: neither triggers layout. */}
      {moving ? (
        <div
          aria-hidden="true"
          className="pointer-events-none fixed inset-x-0 top-0 z-50 h-0 overflow-visible"
        >
          <span className="absolute top-0 left-0 motion-safe:animate-[traverse_26s_linear_infinite]">
            {/* The thread it descends on, so it reads as hanging from the top
                of the window rather than floating in it. */}
            <span className="mx-auto block h-4 w-px bg-current opacity-40" />
            <svg
              viewBox="0 0 60 40"
              className="block h-10 w-14 motion-safe:animate-[bob_1.8s_ease-in-out_infinite]"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            >
              {/* Legs first, so the body sits over where they attach. Four a
                  side, alternating between two gaits half a cycle apart —
                  legs moving in unison reads as a twitch, not a walk. */}
              <g className="spider-legs">
                <g className="leg leg-a" style={{ transformOrigin: "30px 17px" }}>
                  <path d="M30 17 L20 8 L13 4" />
                  <path d="M31 17 L24 6 L20 1" />
                </g>
                <g className="leg leg-b" style={{ transformOrigin: "32px 17px" }}>
                  <path d="M32 17 L30 6 L28 1" />
                  <path d="M33 17 L36 7 L39 2" />
                </g>
                <g className="leg leg-b" style={{ transformOrigin: "30px 23px" }}>
                  <path d="M30 23 L20 32 L13 36" />
                  <path d="M31 23 L24 34 L20 39" />
                </g>
                <g className="leg leg-a" style={{ transformOrigin: "32px 23px" }}>
                  <path d="M32 23 L30 34 L28 39" />
                  <path d="M33 23 L36 33 L39 38" />
                </g>
              </g>

              {/* Abdomen behind, cephalothorax in front — the spider faces the
                  way it is walking. */}
              <ellipse cx="22" cy="20" rx="11" ry="8" fill="currentColor" stroke="none" />
              <ellipse cx="37" cy="20" rx="7" ry="6" fill="currentColor" stroke="none" />
              {/* Eyes, on the leading edge. Small, but they are what make the
                  direction of travel readable at a glance. */}
              <circle cx="41" cy="18" r="1.4" fill="var(--color-paper, #fff)" stroke="none" />
              <circle cx="41" cy="22" r="1.4" fill="var(--color-paper, #fff)" stroke="none" />
            </svg>
          </span>
        </div>
      ) : null}

      <p className={`flex items-center gap-1.5 font-mono text-xs ${tone}`} title={title}>
        {/* The header keeps a small spider as the label's marker. The one that
            walks the top of the window is the signal; this is its legend.

            Idle, it sleeps: a slow breath and three z's drifting up. Nothing is
            crawling and nothing is wrong, and a still spider alone reads the
            same as a broken one. Only the genuinely idle state gets this —
            `queued` is waiting on a worker, which is a thing to fix rather
            than a thing to rest through. */}
        <span aria-hidden="true" className="relative inline-block">
          <span
            className={asleep ? "inline-block motion-safe:animate-[breathe_3.6s_ease-in-out_infinite]" : "inline-block"}
          >
            🕷
          </span>
          {asleep ? (
            <span className="pointer-events-none absolute -top-1 -right-1 select-none">
              <span className="absolute motion-safe:animate-[snore_3.6s_ease-in-out_infinite]">z</span>
              <span className="absolute motion-safe:animate-[snore_3.6s_ease-in-out_infinite] [animation-delay:1.2s]">
                z
              </span>
              <span className="absolute motion-safe:animate-[snore_3.6s_ease-in-out_infinite] [animation-delay:2.4s]">
                z
              </span>
            </span>
          ) : null}
        </span>
        <span>{label}</span>
      </p>

      {/* Keyframes live here rather than in the global sheet: this is the only
          thing that uses them, and a reader of this file should not have to go
          looking for what "traverse" means. `motion-safe:` on the animated
          elements is what honours a reduced-motion preference — the spider then
          does not appear at all, and the header label still says `crawling`. */}
      <style>{`
        @keyframes traverse {
          from { transform: translateX(-4rem); }
          to   { transform: translateX(calc(100vw + 4rem)); }
        }
        @keyframes bob {
          0%, 100% { transform: translateY(0); }
          50%      { transform: translateY(2px); }
        }
        @keyframes breathe {
          0%, 100% { transform: scale(1); opacity: 0.75; }
          50%      { transform: scale(1.08); opacity: 1; }
        }
        /* Up and out, fading as it goes. Staggered by animation-delay above so
           the three z's trail rather than move as one block. */
        @keyframes snore {
          0%   { transform: translate(0, 0) scale(0.6); opacity: 0; }
          20%  { opacity: 0.9; }
          100% { transform: translate(6px, -10px) scale(1); opacity: 0; }
        }
        @keyframes step-a {
          0%, 100% { transform: rotate(-7deg); }
          50%      { transform: rotate(7deg); }
        }
        @keyframes step-b {
          0%, 100% { transform: rotate(7deg); }
          50%      { transform: rotate(-7deg); }
        }
        .spider-legs .leg { transform-box: view-box; }
        @media (prefers-reduced-motion: no-preference) {
          .spider-legs .leg-a { animation: step-a 1.8s ease-in-out infinite; }
          .spider-legs .leg-b { animation: step-b 1.8s ease-in-out infinite; }
        }
      `}</style>
    </>
  );
}
