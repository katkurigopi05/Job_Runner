"use client";

import Link from "next/link";
import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { ApplicationStatus } from "@/lib/api";
import { StatusPill, STATUS_DOT, STATUS_LABEL } from "@/components/status";
import { Icon } from "@/components/icon";

/**
 * Live application status counts, from `GET /api/events/applications`.
 *
 * The dashboard is a server component and stays one: it renders the counts it
 * read, and this replaces them when the API says something moved. The server
 * render is therefore both the first paint and the fallback — if the stream
 * never connects, or drops and cannot get back, the page shows exactly what it
 * showed before any of this existed, which is the state of the world as of the
 * last navigation.
 *
 * Read through the same `/api` rewrite as everything else, so the API keeps
 * refusing non-loopback callers and needs no CORS (CLAUDE.md §3).
 *
 * ## Why counts are assigned, never adjusted
 *
 * Each event carries the whole counts map as the server read it. A client that
 * applied deltas — `+1 running, -1 queued` — drifts the moment one message is
 * missed, and nothing on the page could tell that it had. Assigning a snapshot
 * cannot drift: the worst case of a missed message is being one update stale,
 * which the next event fixes.
 */

export type StatusCounts = Record<ApplicationStatus, number>;

/** What the page knows, and where it got it. */
interface StreamState {
  counts: StatusCounts;
  /** False while falling back to the server-rendered numbers. */
  live: boolean;
}

const StatusStreamContext = createContext<StreamState | null>(null);

/** How long to wait before re-opening a stream that closed, in ms. */
const RETRY_MS = 5_000;

interface StatusEvent {
  counts: StatusCounts | null;
}

export function StatusStreamProvider({
  initial,
  children,
}: {
  initial: StatusCounts;
  children: React.ReactNode;
}) {
  const [counts, setCounts] = useState<StatusCounts>(initial);
  const [live, setLive] = useState(false);

  // The server-rendered numbers are the truth on every navigation: a stream
  // opened on the previous page must not hold stale counts over a fresh read.
  useEffect(() => {
    setCounts(initial);
  }, [initial]);

  useEffect(() => {
    // EventSource reconnects on its own, but only for a *dropped* connection —
    // an HTTP error closes it for good. So the retry below covers the case its
    // own reconnect does not, and `live` tells the reader which of the two
    // sources they are looking at.
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;

    const handle = (message: MessageEvent<string>) => {
      try {
        const body = JSON.parse(message.data) as StatusEvent;
        if (body.counts) setCounts(body.counts);
      } catch {
        // A frame we cannot parse is not a reason to tear down the stream or
        // to blank the page: keep the last good numbers.
      }
    };

    const connect = () => {
      if (stopped) return;
      source = new EventSource("/api/events/applications");
      source.addEventListener("ready", handle as EventListener);
      source.addEventListener("status", handle as EventListener);
      source.onopen = () => setLive(true);
      source.onerror = () => {
        setLive(false);
        source?.close();
        source = null;
        if (!stopped) retry = setTimeout(connect, RETRY_MS);
      };
    };

    connect();
    return () => {
      stopped = true;
      if (retry) clearTimeout(retry);
      source?.close();
    };
  }, []);

  const value = useMemo(() => ({ counts, live }), [counts, live]);
  return <StatusStreamContext.Provider value={value}>{children}</StatusStreamContext.Provider>;
}

/**
 * The live counts, or the server-rendered ones when there is no provider.
 *
 * `fallback` is what the server rendered. A consumer outside the provider gets
 * it back unchanged rather than an error, so a panel can be moved onto a page
 * that does not stream without becoming a crash.
 */
export function useStatusCounts(fallback: StatusCounts): StreamState {
  return useContext(StatusStreamContext) ?? { counts: fallback, live: false };
}

/**
 * The pipeline row, live.
 *
 * Renders from the stream when it is connected and from `initial` — the
 * server-rendered counts — when it is not. The two are the same shape on
 * purpose: there is no third rendering for "stream down", because a dashboard
 * that changes how it looks when a convenience fails teaches the reader to
 * distrust it.
 */
export function LivePipeline({ initial, order }: { initial: StatusCounts; order: ApplicationStatus[] }) {
  const { counts, live } = useStatusCounts(initial);
  const total = order.reduce((sum, status) => sum + (counts[status] ?? 0), 0);
  const description = order.map((status) => `${(counts[status] ?? 0).toLocaleString("en-US")} ${STATUS_LABEL[status].toLowerCase()}`).join(", ");
  return (
    <div aria-live="polite" title={live ? "Updating live" : "Updated when this page was loaded"}>
      <div className="mb-3 flex h-2 gap-0.5 overflow-hidden rounded-full bg-rule" role="img"
        aria-label={total ? description : "No applications yet"}>
        {order.filter((status) => counts[status] > 0).map((status) => (
          <span key={status} className={status === "failed" ? "bg-ink-soft/45" : STATUS_DOT[status]}
            style={{ flex: counts[status] }} />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-2">
        {order.map((status) => (
          <span key={status} className="inline-flex items-center gap-2">
            {counts[status] ? <StatusPill status={status} /> : (
              <span className="inline-flex items-center gap-2 text-label text-ink-faint">
                <span aria-hidden="true" className="status-dot bg-rule" />{STATUS_LABEL[status]}
              </span>
            )}
            <span className="font-mono text-xs text-ink-faint tabular-nums">{(counts[status] ?? 0).toLocaleString("en-US")}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

/** How many applications are waiting on the owner, live. Just the number. */
export function LiveWaitingCount({ initial }: { initial: StatusCounts }) {
  const { counts } = useStatusCounts(initial);
  return <>{waitingFrom(counts).toLocaleString("en-US")}</>;
}

/** The headline and its link, which say different things at zero.
 *
 * The markup lives here rather than coming in as a render prop: a server
 * component cannot hand a function to a client component, and Next fails the
 * whole route with "Functions cannot be passed directly to Client Components"
 * when it tries. Found by loading the page, which is the only way to find it —
 * `tsc` and the linter are both happy with the version that 500s.
 */
export function LiveWaitingHeadline({ initial }: { initial: StatusCounts }) {
  const { counts } = useStatusCounts(initial);
  const waiting = waitingFrom(counts);
  return (
    <div className="desk-heading">
      <div>
        <h1>{waiting > 0 ? `${waiting.toLocaleString("en-US")} waiting on you` : "Nothing waiting on you"}</h1>
        <p className="desk-subtitle">
          {waiting > 0
            ? waiting === 1 ? "Your application is held until you approve it." : "Your applications are held until you approve them."
            : "The queue is clear. You approve every application before it is sent."}
        </p>
      </div>
      {waiting > 0 ? <Link href="/finish" className="primary-action">Work the queue <Icon name="arrow" /></Link> : null}
    </div>
  );
}

/** The two statuses that are asking the owner for something — `NEEDS_OWNER`. */
function waitingFrom(counts: StatusCounts): number {
  return (counts.needs_review ?? 0) + (counts.needs_otp ?? 0);
}
