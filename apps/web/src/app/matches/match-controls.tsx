"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { api, type QueueStart } from "@/lib/api";

export function MatchControls() {
  const router = useRouter();
  const cache = useQueryClient();
  const [notice, setNotice] = useState("");
  const wasPending = useRef(false);
  const crawl = useQuery({
    queryKey: ["crawl-status"], queryFn: api.crawlStatus, refetchInterval: 5000,
  });
  const rescore = useQuery({
    queryKey: ["rescore-status"], queryFn: api.rescoreStatus, refetchInterval: 3000,
  });

  const refresh = () => {
    void cache.invalidateQueries();
    router.refresh();
  };
  useEffect(() => {
    if (!rescore.data) return;
    const pending = rescore.data.pending > 0;
    if (wasPending.current && !pending) {
      setNotice(rescore.data.last_status === "done"
        ? "Rescore complete. Matches refreshed."
        : "Rescore did not complete. Check the worker task log.");
      void cache.invalidateQueries({ queryKey: ["matches"] });
      router.refresh();
    }
    wasPending.current = pending;
  }, [rescore.data, cache, router]);

  const started = (action: string, result: QueueStart) => {
    if (action === "Rescore") wasPending.current = true;
    setNotice(`${action} ${result.queued ? "queued" : "already waiting"}. ${
      result.worker_alive ? "Worker is alive." : "No live worker; start make worker to run it."
    }`);
    refresh();
  };
  const runCrawl = useMutation({
    mutationFn: api.startCrawl,
    onSuccess: (result) => started("Crawl", result),
    onError: (error: Error) => setNotice(error.message),
  });
  const runRescore = useMutation({
    mutationFn: api.rescoreMatches,
    onSuccess: (result) => started("Rescore", result),
    onError: (error: Error) => setNotice(error.message),
  });
  return (
    <div className="max-w-sm space-y-2">
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={runCrawl.isPending || crawl.data?.running}
          onClick={() => runCrawl.mutate()}>
          {runCrawl.isPending ? "Queuing crawl…" : "Run Crawl"}
        </Button>
        <Button size="sm" variant="outline" disabled={runRescore.isPending || (rescore.data?.pending ?? 0) > 0}
          onClick={() => runRescore.mutate()}>
          {runRescore.isPending ? "Queuing rescore…" : "Rescore Matches"}
        </Button>
      </div>
      <p role="status" aria-live="polite" className="text-xs text-ink-soft">
        {notice || (rescore.data?.pending
          ? (rescore.data.worker_alive ? "Rescore queued or running; worker alive." : "Rescore waiting for a worker. Start make worker.")
          : crawl.data?.stalled ? "Crawl waiting for a worker. Start make worker."
          : crawl.data?.running ? "Crawl in progress."
          : "Crawl visits company boards. Rescore uses your current profiles.")}
      </p>
      {crawl.isError || rescore.isError ? (
        <p role="alert" className="text-xs text-stop">Could not read task status. Refresh to retry.</p>
      ) : null}
    </div>
  );
}
