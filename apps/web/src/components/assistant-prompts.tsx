import { Icon } from "@/components/icon";

const PROMPTS = [
  { icon: "review", label: "What needs me?", text: "Which applications are waiting on me right now?" },
  { icon: "pipeline", label: "How's it going?", text: "Summarize where my applications stand." },
  { icon: "mail", label: "Any replies?", text: "Have I had any replies, and what did they say?" },
  { icon: "matches", label: "Find new jobs", text: "Find new jobs" },
  { icon: "compass", label: "What am I lagging on?", text: "Compared with my résumés, which skills do my top matches ask for that I am missing?" },
] as const;

export function AssistantPrompts({ busy, onPrompt }: { busy: boolean; onPrompt: (text: string) => void }) {
  return (
    <div className="flex flex-wrap gap-2 border-b border-rule p-3">
      {PROMPTS.map((prompt) => (
        <button key={prompt.label} type="button" disabled={busy} onClick={() => onPrompt(prompt.text)}
          className="inline-flex items-center gap-2 rounded-lg px-2 py-1 text-label text-ink-soft transition-colors hover:bg-paper-high hover:text-ink disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink-soft">
          <Icon name={prompt.icon} />{prompt.label}
        </button>
      ))}
    </div>
  );
}
