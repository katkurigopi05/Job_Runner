"use client";

import { Check, Pencil, X } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { StudioBullet, StudioLine, StudioPreview } from "@/lib/studio-api";

type Origin = StudioLine["origin"] | "edit";

const ORIGIN_LABEL: Record<Origin, string> = {
  resume: "your résumé",
  internship: "internship",
  certification: "certification",
  rewrite: "rewrite",
  edit: "your edit",
};

const ORIGIN_VARIANT: Record<Origin, "default" | "go" | "attn" | "wait"> = {
  resume: "default",
  internship: "go",
  certification: "go",
  rewrite: "wait",
  edit: "attn",
};

const STATUS_VARIANT: Record<
  StudioBullet["status"],
  "default" | "go" | "stop" | "wait"
> = {
  kept: "default",
  "not asked": "default",
  rewritten: "go",
  refused: "stop",
  "provider failed": "wait",
};

type DraftViewProps = {
  preview: StudioPreview;
  sections: Record<string, string[]>;
  ownerEdits: string[];
  onChange: (sections: Record<string, string[]>, ownerEdits: string[]) => void;
};

function EditableLine({
  text,
  origin,
  original,
  onSave,
  onRemove,
}: {
  text: string;
  origin: Origin;
  original: string | null;
  onSave: (text: string) => void;
  onRemove: () => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  if (draft !== null) {
    return (
      <li className="flex items-start gap-2 py-1.5">
        <label className="flex-1">
          <span className="sr-only">Edit line</span>
          <textarea
            className="min-h-16 w-full rounded-[var(--radius)] border border-attn/50 bg-paper px-2 py-1 text-sm"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
        </label>
        <Button
          size="icon"
          variant="accent"
          aria-label="Keep this edit"
          onClick={() => {
            onSave(draft.trim());
            setDraft(null);
          }}
        >
          <Check aria-hidden />
        </Button>
        <Button
          size="icon"
          variant="ghost"
          aria-label="Cancel"
          onClick={() => setDraft(null)}
        >
          <X aria-hidden />
        </Button>
      </li>
    );
  }
  return (
    <li className="group flex items-start gap-3 py-1.5">
      <div className="min-w-0 flex-1">
        <p className="text-sm text-ink">{text}</p>
        {original ? (
          <p className="mt-0.5 text-xs text-ink-faint line-through">
            {original}
          </p>
        ) : null}
      </div>
      <Badge
        variant={ORIGIN_VARIANT[origin]}
        className="shrink-0 font-mono text-[10px]"
      >
        {ORIGIN_LABEL[origin]}
      </Badge>
      <Button
        size="icon"
        variant="ghost"
        aria-label={`Edit: ${text}`}
        onClick={() => setDraft(text)}
      >
        <Pencil aria-hidden />
      </Button>
      <Button
        size="icon"
        variant="ghost"
        aria-label={`Remove: ${text}`}
        onClick={onRemove}
      >
        <X aria-hidden />
      </Button>
    </li>
  );
}

export function DraftView({
  preview,
  sections,
  ownerEdits,
  onChange,
}: DraftViewProps) {
  const known = new Map<string, StudioLine>();
  for (const lines of Object.values(preview.lines)) {
    for (const line of lines) known.set(line.text, line);
  }
  const edited = new Set(ownerEdits);

  const replaceLine = (section: string, index: number, text: string) => {
    const lines = sections[section] ?? [];
    const next = text
      ? lines.map((line, at) => (at === index ? text : line))
      : lines.filter((_, at) => at !== index);
    const nextEdits =
      text && !known.has(text)
        ? [...ownerEdits.filter((e) => e !== lines[index]), text]
        : ownerEdits;
    onChange({ ...sections, [section]: next }, nextEdits);
  };

  return (
    <section aria-labelledby="draft-heading" className="space-y-6">
      <h2 id="draft-heading" className="font-display text-xl">
        The draft, line by line
      </h2>
      {Object.entries(sections).map(([section, lines]) => (
        <div key={section}>
          <h3 className="font-mono text-xs uppercase tracking-widest text-ink-soft">
            {section}
          </h3>
          <ul className="mt-1 divide-y divide-rule">
            {lines.map((text, index) => {
              const line = known.get(text);
              const origin: Origin = edited.has(text)
                ? "edit"
                : (line?.origin ?? "edit");
              return (
                <EditableLine
                  key={`${section}-${index}-${text}`}
                  text={text}
                  origin={origin}
                  original={
                    origin === "rewrite" ? (line?.original ?? null) : null
                  }
                  onSave={(next) => replaceLine(section, index, next)}
                  onRemove={() => replaceLine(section, index, "")}
                />
              );
            })}
          </ul>
        </div>
      ))}
    </section>
  );
}

export function BulletTrace({ bullets }: { bullets: StudioBullet[] }) {
  const shown = bullets.filter(
    (b) => b.status !== "not asked" || b.targets.length > 0,
  );
  if (shown.length === 0) {
    return (
      <p className="text-sm text-ink-soft">
        No bullet can truthfully gain a term this posting asks for, so nothing
        was sent to a model.
      </p>
    );
  }
  return (
    <ol className="space-y-4">
      {shown.map((bullet) => (
        <li
          key={bullet.ref + bullet.original}
          className="border-l-2 border-rule pl-3"
        >
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={STATUS_VARIANT[bullet.status]}>
              {bullet.status}
            </Badge>
            <span className="font-mono text-[11px] text-ink-faint">
              {bullet.ref}
            </span>
          </div>
          <p className="mt-1 text-sm text-ink">{bullet.tailored}</p>
          {bullet.tailored !== bullet.original ? (
            <p className="text-xs text-ink-faint line-through">
              {bullet.original}
            </p>
          ) : null}
          {bullet.targets.length > 0 ? (
            <ul className="mt-1 space-y-0.5 text-xs text-ink-soft">
              {bullet.targets.map((target) => (
                <li key={target.term}>
                  <span className="font-medium text-ink">{target.term}</span> —
                  backed by{" "}
                  {target.kind === "repository"
                    ? "a linked repository"
                    : `“${target.evidence}”`}
                  {target.kind === "practice" ? " in this bullet" : ""}
                </li>
              ))}
            </ul>
          ) : null}
          {bullet.reason ? (
            <p className="mt-1 text-xs text-stop">Refused: {bullet.reason}</p>
          ) : null}
        </li>
      ))}
    </ol>
  );
}
