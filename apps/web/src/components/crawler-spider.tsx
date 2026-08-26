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
 * - **idle** — nothing queued. The spider is still and faint, and the label
 *   carries the age of the newest posting, which is the number that actually
 *   answers "are my results current".
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

  const { label, tone, moving, title } = status.running
    ? {
        label: "crawling",
        tone: "text-go",
        moving: true,
        title: "A crawl is running. New postings will appear in /matches when it finishes.",
      }
    : status.stalled
      ? {
          label: `${status.pending} queued`,
          tone: "text-attn",
          moving: false,
          title:
            "A crawl is queued but no worker is draining it. Start one with: make worker",
        }
      : {
          label: freshness ? `postings ${freshness}` : "idle",
          tone: "text-ink-faint",
          moving: false,
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

          Fixed and `pointer-events-none` so it crosses over the page without
          intercepting a click on whatever it passes. `translateX` only, per
          the project's animation rules: it never triggers layout. */}
      {moving ? (
        <div
          aria-hidden="true"
          className="pointer-events-none fixed inset-x-0 top-0 z-50 h-0 overflow-visible"
        >
          <span className="absolute top-0 left-0 motion-safe:animate-[traverse_9s_linear_infinite]">
            {/* The thread it descends on, so the spider reads as hanging from
                the top of the window rather than floating in it. */}
            <span className="mx-auto block h-3 w-px bg-current opacity-30" />
            <span className="block text-base leading-none motion-safe:animate-[bob_0.7s_ease-in-out_infinite]">
              🕷
            </span>
          </span>
        </div>
      ) : null}

      <p className={`flex items-center gap-1.5 font-mono text-xs ${tone}`} title={title}>
        {/* The header keeps a still spider as the label's marker. The moving
            one above is the signal; this one is the legend for it. */}
        <span aria-hidden="true" className="inline-block">
          🕷
        </span>
        <span>{label}</span>
      </p>

      {/* Keyframes live here rather than in the global sheet: this is the only
          thing that uses them, and a reader of this file should not have to go
          looking for what "traverse" means. `motion-safe:` on both is what
          honours a reduced-motion preference — the spider then does not appear
          at all, and the header label still says `crawling`. */}
      <style>{`
        @keyframes traverse {
          from { transform: translateX(-2rem); }
          to   { transform: translateX(calc(100vw + 2rem)); }
        }
        @keyframes bob {
          0%, 100% { transform: translateY(0) rotate(-6deg); }
          50%      { transform: translateY(2px) rotate(6deg); }
        }
      `}</style>
    </>
  );
}
