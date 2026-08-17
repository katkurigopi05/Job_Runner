import type { ApiError } from "@/lib/api";

/**
 * A local tool's most common failure is "the other half is not running", and
 * the fix is a command. Saying which command beats a stack trace.
 */
const FIX: Record<string, string> = {
  api_unreachable: "make api",
};

export function ErrorPanel({ error }: { error: ApiError }) {
  const fix = FIX[error.code];
  return (
    <div className="border border-stop/40 bg-stop-soft px-6 py-6">
      <h1 className="font-display text-2xl text-stop">Cannot read the queue</h1>
      <p className="mt-3 max-w-prose text-ink-soft">{error.message}</p>
      {fix ? (
        <pre className="mt-5 inline-block rounded-md border border-rule bg-paper px-4 py-2 font-mono text-sm">
          {fix}
        </pre>
      ) : null}
      <p className="mt-5 font-mono text-xs text-ink-faint">
        {error.code} · {error.status}
      </p>
    </div>
  );
}
