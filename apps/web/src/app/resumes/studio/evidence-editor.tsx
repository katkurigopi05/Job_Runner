"use client";

import { useMutation } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import {
  splitList,
  studioApi,
  type Certification,
  type Evidence,
  type Internship,
} from "@/lib/studio-api";

const FIELD =
  "w-full rounded-[var(--radius)] border border-rule bg-paper px-2.5 py-1.5 text-sm text-ink placeholder:text-ink-faint";

const BLANK_INTERNSHIP: Internship = {
  company: "",
  role: "",
  location: null,
  started: "",
  ended: null,
  stack: [],
  bullets: [],
  verified: false,
};

const BLANK_CERTIFICATION: Certification = {
  name: "",
  issuer: "",
  issued: null,
  expires: null,
  credential_id: null,
  skills: [],
  verified: false,
};

interface EvidenceEditorProps {
  profileId: string;
  evidence: Evidence;
  onSaved: (evidence: Evidence) => void;
}

function replaced<T>(list: T[], index: number, next: T): T[] {
  return list.map((item, at) => (at === index ? next : item));
}

function TextField(props: {
  label: string;
  value: string | null;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="grid gap-1 text-xs text-ink-soft">
      {props.label}
      <input
        className={FIELD}
        value={props.value ?? ""}
        placeholder={props.placeholder}
        onChange={(event) => props.onChange(event.target.value)}
      />
    </label>
  );
}

function Confirm(props: {
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="inline-flex items-center gap-2 text-xs text-ink">
      <input
        type="checkbox"
        className="accent-go"
        checked={props.checked}
        onChange={(event) => props.onChange(event.target.checked)}
      />
      I confirm this is accurate. Only confirmed records reach a résumé.
    </label>
  );
}

export function EvidenceEditor({
  profileId,
  evidence,
  onSaved,
}: EvidenceEditorProps) {
  const [internships, setInternships] = useState<Internship[]>(
    evidence.internships,
  );
  const [certifications, setCertifications] = useState<Certification[]>(
    evidence.certifications,
  );
  const [dirty, setDirty] = useState(false);

  const save = useMutation({
    mutationFn: () =>
      studioApi.saveEvidence(profileId, { internships, certifications }),
    onSuccess: (saved) => {
      setInternships(saved.internships);
      setCertifications(saved.certifications);
      setDirty(false);
      onSaved(saved);
    },
  });

  const editInternship = (index: number, patch: Partial<Internship>) => {
    setInternships((list) =>
      replaced(list, index, { ...list[index], ...patch }),
    );
    setDirty(true);
  };
  const editCertification = (index: number, patch: Partial<Certification>) => {
    setCertifications((list) =>
      replaced(list, index, { ...list[index], ...patch }),
    );
    setDirty(true);
  };

  return (
    <section aria-labelledby="evidence-heading" className="space-y-6">
      <header className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 id="evidence-heading" className="font-display text-xl">
          Internships and certifications
        </h2>
        <p className="max-w-prose text-xs text-ink-soft">
          Your own words, printed as you type them. An internship&rsquo;s tools
          may be named in that internship&rsquo;s bullets and no one
          else&rsquo;s.
        </p>
      </header>

      <div className="space-y-4">
        {internships.map((item, index) => (
          <fieldset
            key={item.id ?? `new-${index}`}
            className="grid gap-3 border-t border-rule pt-4 sm:grid-cols-2"
          >
            <legend className="sr-only">Internship {index + 1}</legend>
            <TextField
              label="Company"
              value={item.company}
              onChange={(v) => editInternship(index, { company: v })}
            />
            <TextField
              label="Role"
              value={item.role}
              onChange={(v) => editInternship(index, { role: v })}
            />
            <TextField
              label="Started"
              value={item.started}
              placeholder="Jun 2025"
              onChange={(v) => editInternship(index, { started: v })}
            />
            <TextField
              label="Ended"
              value={item.ended}
              placeholder="Aug 2025"
              onChange={(v) => editInternship(index, { ended: v || null })}
            />
            <TextField
              label="Location"
              value={item.location}
              onChange={(v) => editInternship(index, { location: v || null })}
            />
            <TextField
              label="Tools, comma-separated"
              value={item.stack.join(", ")}
              onChange={(v) => editInternship(index, { stack: splitList(v) })}
            />
            <label className="grid gap-1 text-xs text-ink-soft sm:col-span-2">
              What you did, one bullet per line, numbers as they were
              <textarea
                className={`${FIELD} min-h-24`}
                value={item.bullets.join("\n")}
                onChange={(event) =>
                  editInternship(index, {
                    bullets: event.target.value
                      .split("\n")
                      .filter((line) => line.trim()),
                  })
                }
              />
            </label>
            <div className="flex flex-wrap items-center justify-between gap-3 sm:col-span-2">
              <Confirm
                checked={item.verified}
                onChange={(v) => editInternship(index, { verified: v })}
              />
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setInternships((list) =>
                    list.filter((_, at) => at !== index),
                  );
                  setDirty(true);
                }}
              >
                <Trash2 aria-hidden /> Remove
              </Button>
            </div>
          </fieldset>
        ))}
        <Button
          size="sm"
          onClick={() => {
            setInternships((list) => [...list, BLANK_INTERNSHIP]);
            setDirty(true);
          }}
        >
          <Plus aria-hidden /> Add internship
        </Button>
      </div>

      <div className="space-y-4">
        {certifications.map((item, index) => (
          <fieldset
            key={item.id ?? `new-${index}`}
            className="grid gap-3 border-t border-rule pt-4 sm:grid-cols-2"
          >
            <legend className="sr-only">Certification {index + 1}</legend>
            <TextField
              label="Certification"
              value={item.name}
              onChange={(v) => editCertification(index, { name: v })}
            />
            <TextField
              label="Issuer"
              value={item.issuer}
              onChange={(v) => editCertification(index, { issuer: v })}
            />
            <TextField
              label="Issued"
              value={item.issued}
              placeholder="Mar 2025"
              onChange={(v) => editCertification(index, { issued: v || null })}
            />
            <TextField
              label="Expires"
              value={item.expires}
              onChange={(v) => editCertification(index, { expires: v || null })}
            />
            <TextField
              label="Credential ID"
              value={item.credential_id}
              onChange={(v) =>
                editCertification(index, { credential_id: v || null })
              }
            />
            <TextField
              label="Skills it covers, comma-separated"
              value={item.skills.join(", ")}
              onChange={(v) =>
                editCertification(index, { skills: splitList(v) })
              }
            />
            <div className="flex flex-wrap items-center justify-between gap-3 sm:col-span-2">
              <Confirm
                checked={item.verified}
                onChange={(v) => editCertification(index, { verified: v })}
              />
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setCertifications((list) =>
                    list.filter((_, at) => at !== index),
                  );
                  setDirty(true);
                }}
              >
                <Trash2 aria-hidden /> Remove
              </Button>
            </div>
          </fieldset>
        ))}
        <Button
          size="sm"
          onClick={() => {
            setCertifications((list) => [...list, BLANK_CERTIFICATION]);
            setDirty(true);
          }}
        >
          <Plus aria-hidden /> Add certification
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-rule pt-4">
        <Button
          variant="accent"
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? "Saving…" : "Save internships and certifications"}
        </Button>
        <p className="text-xs text-ink-soft" role="status">
          {save.error
            ? save.error instanceof ApiError
              ? save.error.message
              : "Could not save."
            : dirty
              ? "Unsaved. The studio reads what is saved."
              : "Saved."}
        </p>
      </div>
    </section>
  );
}
