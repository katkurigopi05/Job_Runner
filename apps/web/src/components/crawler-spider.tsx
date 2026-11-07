"use client";

import { useEffect, useState } from "react";
import type { CrawlStatus } from "@/lib/api";
import { backlogView, crawlerView } from "@/lib/crawler-state";

/** Slow: this is a local queue, and a crawl runs for minutes, not milliseconds. */
const POLL_MS = 10_000;

/** The drawn spider and the queue's five-state label, without decorative motion.
 * Poll failures replace the last verdict; stale work never looks current.
 */
export function CrawlerSpider() {
  const [status, setStatus] = useState<CrawlStatus | null>(null);
  const [reachable, setReachable] = useState(true);

  useEffect(() => {
    let mounted = true;

    const check = async () => {
      try {
        const res = await fetch("/api/crawl/status", { cache: "no-store" });
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as CrawlStatus;
        if (mounted) {
          setStatus(body);
          setReachable(true);
        }
      } catch {
        // Do not leave the previous moving state on screen. An explicit quiet
        // failure is the only honest answer when the queue cannot be read.
        if (mounted) setReachable(false);
      }
    };

    void check();
    const timer = setInterval(check, POLL_MS);
    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, []);

  const { label, tone, moving, title } = crawlerView(status, reachable);
  // Only from a fresh response: a stale count would read as current.
  const backlog = reachable ? backlogView(status) : null;

  return (
    <div>
      <p className={`flex items-center gap-2 text-xs ${moving ? "text-ink-soft" : tone}`}
        title={title} aria-live="polite">
            <svg
              aria-hidden="true" viewBox="0 0 60 40"
              className="h-5 w-7 shrink-0"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
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
        <span>{label.charAt(0).toUpperCase() + label.slice(1)}</span>
      </p>
      {backlog ? <p className="mt-1 text-xs text-ink-faint" title={backlog.title}>{backlog.text}</p> : null}
    </div>
  );
}
