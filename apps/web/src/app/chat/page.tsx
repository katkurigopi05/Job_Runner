import { Assistant } from "@/components/assistant";

export const metadata = { title: "Assistant · Jobrunner" };

export default function ChatPage() {
  return (
    <div className="space-y-8">
      <header>
        <h1 className="font-display text-display leading-[1.05] tracking-tight">Assistant</h1>
        <p className="mt-3 max-w-prose text-ink-soft">
          Answers from a model running on this machine, grounded in your own data. It will not
          draft work-authorization, sponsorship, employment-history, or salary answers — those are
          copied from your profile word for word, because a wrong one has consequences.
        </p>
      </header>

      {/* Clamped rather than fixed: 34rem was chosen when this panel had one
          header row, and every row added since — the provider picker, the mail
          gate — ate into it until the content no longer fit. A viewport-relative
          height with a floor and a ceiling grows with the panel instead of
          silently overflowing the next time a control is added. */}
      <div className="h-[clamp(28rem,70vh,44rem)]">
        <Assistant />
      </div>

      <p className="max-w-prose font-mono text-xs leading-relaxed text-ink-faint">
        Needs Ollama running locally (<code>ollama serve</code>). If it is not, the assistant says
        so rather than falling back to a cloud provider — chat context is your own data, and §2.8
        covers only the tailoring call.
      </p>
    </div>
  );
}
