import type { ApplicationStatus, FailureReason } from "@/lib/api";

export const STATUS_ORDER: ApplicationStatus[] = ["needs_review", "needs_otp", "running", "queued", "submitted", "failed"];
export const STATUS_DOT: Record<ApplicationStatus, string> = {
  needs_review: "bg-attn", needs_otp: "bg-attn", running: "bg-ink-faint",
  queued: "bg-ink-faint", submitted: "bg-go", failed: "bg-stop",
};
export const STATUS_LABEL: Record<ApplicationStatus, string> = {
  needs_review: "Needs review", needs_otp: "Needs OTP", running: "Running",
  queued: "Queued", submitted: "Submitted", failed: "Failed",
};

/** Clay asks for an owner decision. In-flight states use neutral text. */
export function StatusPill({ status, reason }: { status: ApplicationStatus; reason?: FailureReason | null }) {
  const detail = status !== "failed" ? null : reason === "manual_completion_required" || reason === "unsupported_site"
    ? "Finish by hand" : reason === "site_error" ? "Site error" : null;
  return (
    <span title={reason ? `${STATUS_LABEL[status]}: ${reason.replace(/_/g, " ")}` : undefined} className={`inline-flex items-center gap-2 text-label ${status === "needs_review" || status === "needs_otp" ? "text-attn-text" : "text-ink-soft"}`}>
      <span aria-hidden="true" className={`status-dot ${STATUS_DOT[status]}`} />
      {detail ?? STATUS_LABEL[status]}
      {reason && !detail ? <span className="text-ink-faint">· {reason.replace(/_/g, " ")}</span> : null}
    </span>
  );
}

export function FillRate({ rate }: { rate?: number }) {
  if (rate === undefined) return null;
  const percent = Math.round(rate * 100);
  const met = percent >= 80;
  return (
    <div className="flex items-center gap-3">
      <div className="relative h-1.5 w-28 overflow-hidden rounded-full bg-rule" role="img"
        aria-label={`${percent} percent of fields filled automatically`}>
        <div className={`h-full rounded-full ${met ? "bg-go" : "bg-ink-faint"}`} style={{ width: `${percent}%` }} />
      </div>
      <span className="font-mono text-xs text-ink-soft tabular-nums">{percent.toLocaleString("en-US")}% filled</span>
    </div>
  );
}
