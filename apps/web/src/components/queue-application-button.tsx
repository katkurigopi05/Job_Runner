"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, Send } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { queueApplication } from "@/app/matches/actions";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

interface ApplicationSummary {
  id: string;
  url: string;
  status: string;
}

interface Props {
  postingUrl: string;
  profileId: string;
  candidateId?: string;
  ats?: string | null;
}

const STATUS_LABELS: Record<string, string> = {
  queued: "Queued",
  running: "Tailoring…",
  needs_review: "Ready for review",
  needs_otp: "Needs OTP",
  submitted: "Submitted",
  failed: "Failed",
};

/**
 * Queue a posting for tailoring & pre-submit fill (parks in `/review`).
 *
 * Uses TanStack Query against `/api/applications` (via Next's loopback rewrite)
 * so every match card shares one cached list and reflects queued state live.
 */
export function QueueApplicationButton({
  postingUrl,
  profileId,
  candidateId,
  ats,
}: Props) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [optimistic, setOptimistic] = useState<ApplicationSummary | null>(null);
  const [errorMessage, setErrorMessage] = useState<string>("");

  const { data: applications } = useQuery<ApplicationSummary[]>({
    queryKey: ["applications"],
    queryFn: async () => {
      const res = await fetch("/api/applications", { cache: "no-store" });
      if (!res.ok) return [];
      return (await res.json()) as ApplicationSummary[];
    },
    staleTime: 15_000,
  });

  const existing = useMemo(() => {
    if (optimistic) return optimistic;
    return applications?.find((app) => app.url === postingUrl) ?? null;
  }, [applications, optimistic, postingUrl]);

  const mutation = useMutation({
    mutationFn: () =>
      queueApplication({
        candidateId,
        profileId,
        url: postingUrl,
        ats: ats ?? null,
      }),
    onSuccess: (result) => {
      if (result.ok && result.application) {
        setErrorMessage("");
        setOptimistic({
          id: result.application.id,
          url: result.application.url,
          status: result.application.status,
        });
        void queryClient.invalidateQueries({ queryKey: ["applications"] });
        router.refresh();
        return;
      }
      if (result.duplicate) {
        void queryClient.invalidateQueries({ queryKey: ["applications"] });
      }
      setErrorMessage(result.message);
    },
    onError: () => {
      setErrorMessage("Could not queue this application.");
    },
  });

  if (existing) {
    const label = STATUS_LABELS[existing.status] ?? existing.status;
    const href =
      existing.status === "needs_review" || existing.status === "needs_otp"
        ? "/review"
        : "/applications";
    const variant =
      existing.status === "submitted"
        ? "go"
        : existing.status === "needs_review" || existing.status === "needs_otp"
          ? "attn"
          : existing.status === "failed"
            ? "stop"
            : "wait";

    return (
      <Link href={href} className="inline-flex items-center gap-1.5 hover:opacity-90">
        <Badge variant={variant}>
          <CheckCircle2 className="h-3 w-3" aria-hidden />
          <span>{label}</span>
        </Badge>
      </Link>
    );
  }

  return (
    <div className="inline-flex flex-wrap items-center gap-2">
      <Button
        variant="accent"
        size="sm"
        onClick={() => {
          setErrorMessage("");
          mutation.mutate();
        }}
        disabled={mutation.isPending}
      >
        {mutation.isPending ? (
          <Loader2 className="motion-safe:animate-spin" aria-hidden />
        ) : (
          <Send aria-hidden />
        )}
        <span>{mutation.isPending ? "Queuing…" : "Queue & tailor"}</span>
      </Button>
      {errorMessage ? (
        <span className="text-xs text-attn" role="status">
          {errorMessage}
        </span>
      ) : null}
    </div>
  );
}
