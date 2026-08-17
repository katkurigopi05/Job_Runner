import type { ApplicationStatus, FailureReason } from "@/lib/api";

/* Colour carries meaning here, so it is assigned once and only once.
   Ochre is reserved for the two states that are waiting on the owner — if
   something is ochre on this screen, it is asking for a decision. */
const TONE: Record<ApplicationStatus, string> = {
  needs_review: "bg-attn-soft text-attn border-attn/40",
  needs_otp: "bg-attn-soft text-attn border-attn/40",
  running: "bg-wait-soft text-wait border-wait/40",
  queued: "bg-wait-soft text-wait border-wait/40",
  submitted: "bg-go-soft text-go border-go/40",
  failed: "bg-stop-soft text-stop border-stop/40",
};

const LABEL: Record<ApplicationStatus, string> = {
  needs_review: "needs review",
  needs_otp: "needs otp",
  running: "running",
  queued: "queued",
  submitted: "submitted",
  failed: "failed",
};

export function StatusPill({
  status,
  reason,
}: {
  status: ApplicationStatus;
  reason?: FailureReason | null;
}) {
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 font-mono text-xs ${TONE[status]}`}
    >
      {LABEL[status]}
      {reason ? <span className="opacity-70">· {reason.replace(/_/g, " ")}</span> : null}
    </span>
  );
}

export function FillRate({ rate }: { rate?: number }) {
  if (rate === undefined) return null;
  const percent = Math.round(rate * 100);
  // Gate 2 asks for >=80% filled with zero manual input, so the bar marks that
  // line rather than just showing a proportion.
  const met = percent >= 80;
  return (
    <div className="flex items-center gap-3">
      <div
        className="relative h-1.5 w-28 overflow-hidden rounded-full bg-rule"
        role="img"
        aria-label={`${percent} percent of fields filled automatically`}
      >
        <div
          className={`h-full rounded-full ${met ? "bg-go" : "bg-attn"}`}
          style={{ width: `${percent}%` }}
        />
      </div>
      <span className="font-mono text-xs text-ink-soft">{percent}% filled</span>
    </div>
  );
}
