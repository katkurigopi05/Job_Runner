"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { api, type CrawlStop, type QueueStart, type WorkerControls } from "@/lib/api";

const WORKER_KIND_GROUPS: { label: string; kinds: string[] }[] = [
  { label: "Crawl", kinds: ["crawl", "crawl_company"] },
  { label: "Discovery", kinds: ["discover", "discover_company", "deep_dive_company"] },
  { label: "Interview prep", kinds: ["interview_prep"] },
  { label: "Apply", kinds: ["apply"] },
  { label: "Rescore", kinds: ["rescore"] },
  { label: "Inbox", kinds: ["inbox"] },
];

export function MatchControls() {
  const router = useRouter();
  const cache = useQueryClient();
  const [notice, setNotice] = useState("");
  const [showWorkers, setShowWorkers] = useState(false);
  const wasPending = useRef(false);

  const crawl = useQuery({
    queryKey: ["crawl-status"],
    queryFn: api.crawlStatus,
    refetchInterval: 5000,
  });
  const rescore = useQuery({
    queryKey: ["rescore-status"],
    queryFn: api.rescoreStatus,
    refetchInterval: 3000,
  });
  const controls = useQuery({
    queryKey: ["worker-controls"],
    queryFn: api.workerControls,
    refetchInterval: 5000,
  });

  const refresh = () => {
    void cache.invalidateQueries();
    router.refresh();
  };

  useEffect(() => {
    if (!rescore.data) return;
    const pending = rescore.data.pending > 0;
    if (wasPending.current && !pending) {
      setNotice(
        rescore.data.last_status === "done"
          ? "Rescore complete. Matches refreshed."
          : "Rescore did not complete. Check the worker task log.",
      );
      void cache.invalidateQueries({ queryKey: ["matches"] });
      router.refresh();
    }
    wasPending.current = pending;
  }, [rescore.data, cache, router]);

  const started = (action: string, result: QueueStart) => {
    if (action === "Rescore") wasPending.current = true;
    setNotice(
      `${action} ${result.queued ? "queued" : "already waiting"}. ${
        result.worker_alive
          ? "Worker is alive."
          : "No live worker; start make worker to run it."
      }`,
    );
    refresh();
  };

  const runCrawl = useMutation({
    mutationFn: api.startCrawl,
    onSuccess: (result) => started("Crawl", result),
    onError: (error: Error) => setNotice(error.message),
  });
  const pauseCrawl = useMutation({
    mutationFn: api.pauseCrawl,
    onSuccess: () => {
      setNotice("Crawler paused. Pending tasks remain queued.");
      refresh();
    },
    onError: (error: Error) => setNotice(error.message),
  });
  const resumeCrawl = useMutation({
    mutationFn: api.resumeCrawl,
    onSuccess: (result) => {
      setNotice(
        `Crawler resumed (${result.queued ? "crawl queued" : "queue active"}).`,
      );
      refresh();
    },
    onError: (error: Error) => setNotice(error.message),
  });
  const stopCrawl = useMutation({
    mutationFn: api.stopCrawl,
    onSuccess: (result: CrawlStop) => {
      setNotice(
        `Crawler stopped (${result.cancelled} pending task${result.cancelled === 1 ? "" : "s"} cancelled).`,
      );
      refresh();
    },
    onError: (error: Error) => setNotice(error.message),
  });
  const runRescore = useMutation({
    mutationFn: api.rescoreMatches,
    onSuccess: (result) => started("Rescore", result),
    onError: (error: Error) => setNotice(error.message),
  });
  const saveControls = useMutation({
    mutationFn: api.updateWorkerControls,
    onSuccess: (updated: WorkerControls) => {
      cache.setQueryData(["worker-controls"], updated);
      setNotice(
        `Worker controls saved (max ${updated.max_workers} worker${updated.max_workers === 1 ? "" : "s"}, ${updated.enabled_kinds.length} task kinds active).`,
      );
      refresh();
    },
    onError: (error: Error) => setNotice(error.message),
  });

  const isPaused = Boolean(crawl.data?.paused || controls.data?.crawler_paused);
  const enabledSet = new Set(
    controls.data?.enabled_kinds ?? [
      "rescore",
      "apply",
      "crawl",
      "crawl_company",
      "discover_company",
      "deep_dive_company",
      "discover",
      "inbox",
      "interview_prep",
    ],
  );

  const toggleGroup = (kinds: string[], checked: boolean) => {
    const next = new Set(enabledSet);
    for (const k of kinds) {
      if (checked) next.add(k);
      else next.delete(k);
    }
    saveControls.mutate({ enabled_kinds: Array.from(next) });
  };

  return (
    <div className="max-w-lg space-y-2">
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={runCrawl.isPending || Boolean(crawl.data?.running && !isPaused)}
          onClick={() => runCrawl.mutate()}
        >
          {runCrawl.isPending ? "Queuing crawl…" : "Run Crawl"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={pauseCrawl.isPending || resumeCrawl.isPending}
          onClick={() =>
            isPaused ? resumeCrawl.mutate() : pauseCrawl.mutate()
          }
        >
          {isPaused ? "Resume Crawl" : "Pause Crawl"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={stopCrawl.isPending}
          onClick={() => stopCrawl.mutate()}
        >
          Stop Crawl
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={runRescore.isPending || (rescore.data?.pending ?? 0) > 0}
          onClick={() => runRescore.mutate()}
        >
          {runRescore.isPending ? "Queuing rescore…" : "Rescore Matches"}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          aria-expanded={showWorkers}
          onClick={() => setShowWorkers((prev) => !prev)}
        >
          Workers ({controls.data?.alive_workers ?? 0}/{controls.data?.max_workers ?? 1})
        </Button>
      </div>

      {showWorkers ? (
        <div
          role="region"
          aria-label="Worker controls"
          className="rounded-lg border border-rule bg-paper-raised p-3 text-xs space-y-2"
        >
          <div className="flex items-center justify-between gap-3">
            <label htmlFor="max-workers-select" className="text-ink-soft font-medium">
              Max workers
            </label>
            <select
              id="max-workers-select"
              aria-label="Max workers"
              value={controls.data?.max_workers ?? 1}
              disabled={saveControls.isPending}
              onChange={(e) =>
                saveControls.mutate({ max_workers: Number(e.target.value) })
              }
              className="rounded border border-rule bg-paper px-2 py-1 text-xs tabular-nums"
            >
              {[1, 2, 3, 4, 6, 8].map((n) => (
                <option key={n} value={n}>
                  {n} {n === 1 ? "worker" : "workers"}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <p className="text-ink-faint">Active worker queues:</p>
            <div className="flex flex-wrap gap-3">
              {WORKER_KIND_GROUPS.map((group) => {
                const checked = group.kinds.every((k) => enabledSet.has(k));
                return (
                  <label key={group.label} className="inline-flex items-center gap-1.5">
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={saveControls.isPending}
                      onChange={(e) => toggleGroup(group.kinds, e.target.checked)}
                      className="size-3.5 accent-ink"
                    />
                    <span>{group.label}</span>
                  </label>
                );
              })}
            </div>
          </div>
        </div>
      ) : null}

      <p role="status" aria-live="polite" className="text-xs text-ink-soft">
        {notice ||
          (rescore.data?.pending
            ? rescore.data.worker_alive
              ? "Rescore queued or running; worker alive."
              : "Rescore waiting for a worker. Start make worker."
            : isPaused
              ? "Crawler paused. Click Resume Crawl to continue."
              : crawl.data?.stalled
                ? "Crawl waiting for a worker. Start make worker."
                : crawl.data?.running
                  ? "Crawl in progress."
                  : "Crawl visits company boards. Rescore uses your current profiles.")}
      </p>
      {crawl.isError || rescore.isError ? (
        <p role="alert" className="text-xs text-stop">
          Could not read task status. Refresh to retry.
        </p>
      ) : null}
    </div>
  );
}
