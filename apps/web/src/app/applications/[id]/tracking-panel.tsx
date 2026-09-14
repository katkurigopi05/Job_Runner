"use client";

import { useState, useTransition } from "react";
import type { ApplicationTask, TaskKind, Tracking } from "@/lib/api";
import { addContact, addTask, deleteTask, removeContact, updateTask } from "./tracking-actions";

/**
 * People and tasks on one application.
 *
 * Everything here is the owner's record. Nothing on this panel sends a
 * message: a follow-up task is a reminder to write one yourself, and a
 * reminder is a notification on this machine.
 */

const FIELD =
  "rounded-[var(--radius)] border border-rule bg-paper px-3 py-1.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-attn";

const KINDS: { value: TaskKind; label: string }[] = [
  { value: "interview", label: "interview" },
  { value: "assessment", label: "assessment" },
  { value: "follow_up", label: "follow-up (you send it)" },
  { value: "prep", label: "prep" },
  { value: "other", label: "other" },
];

const REMINDERS = [
  { value: "", label: "no reminder" },
  { value: "15", label: "15 min before" },
  { value: "60", label: "1 hour before" },
  { value: "1440", label: "1 day before" },
];

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "no date";
}

function TaskRow({ applicationId, task }: { applicationId: string; task: ApplicationTask }) {
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const done = task.completed_at !== null;
  const overdue = !done && task.due_at !== null && new Date(task.due_at) < new Date();

  const act = (work: () => Promise<{ ok: boolean; message?: string }>) =>
    start(async () => {
      const outcome = await work();
      setError(outcome.ok ? null : (outcome.message ?? "could not save"));
    });

  return (
    <li className={`rounded-[var(--radius)] border border-rule bg-paper-raised px-4 py-3 ${done ? "opacity-60" : ""}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className={`text-sm ${done ? "line-through" : "text-ink"}`}>
          <span className="mr-2 font-mono text-xs uppercase tracking-widest text-ink-faint">
            {task.kind.replace("_", " ")}
          </span>
          {task.title}
        </p>
        <p className={`font-mono text-xs ${overdue ? "text-stop" : "text-ink-faint"}`}>
          {overdue ? "overdue · " : ""}
          {when(task.due_at)}
          {task.source === "inbox" ? " · from a reply" : ""}
        </p>
      </div>
      {task.checklist.length > 0 ? (
        <ul className="mt-2 space-y-1">
          {task.checklist.map((item, index) => (
            <li key={`${index}-${item.text}`}>
              <label className="inline-flex items-center gap-2 text-sm text-ink-soft">
                <input
                  type="checkbox"
                  checked={item.done}
                  disabled={pending}
                  onChange={() =>
                    act(() =>
                      updateTask(applicationId, task.id, {
                        checklist: task.checklist.map((entry, i) =>
                          i === index ? { ...entry, done: !entry.done } : entry,
                        ),
                      }),
                    )
                  }
                  className="h-3.5 w-3.5 accent-[var(--color-go)]"
                />
                <span className={item.done ? "line-through" : ""}>{item.text}</span>
              </label>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="mt-2 flex flex-wrap gap-3">
        <button
          type="button"
          disabled={pending}
          onClick={() => act(() => updateTask(applicationId, task.id, { completed: !done }))}
          className="font-mono text-xs text-ink-soft hover:text-go disabled:opacity-50"
        >
          {done ? "reopen" : "mark done"}
        </button>
        <button
          type="button"
          disabled={pending}
          onClick={() => act(() => deleteTask(applicationId, task.id))}
          className="font-mono text-xs text-ink-faint hover:text-stop disabled:opacity-50"
        >
          delete
        </button>
        {error ? <span className="text-xs text-stop">{error}</span> : null}
      </div>
    </li>
  );
}

function AddTask({ applicationId }: { applicationId: string }) {
  const [kind, setKind] = useState<TaskKind>("interview");
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [remind, setRemind] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();

  return (
    <form
      className="flex flex-wrap items-end gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        start(async () => {
          const outcome = await addTask(applicationId, {
            kind,
            title,
            // A datetime-local value is read in the browser's own time zone, so
            // the instant sent to the API is the one the owner meant.
            dueAt: due ? new Date(due).toISOString() : null,
            remindMinutesBefore: remind ? Number(remind) : null,
          });
          setMessage(outcome.ok ? "added" : outcome.message);
          if (outcome.ok) {
            setTitle("");
            setDue("");
          }
        });
      }}
    >
      <select aria-label="task kind" value={kind} onChange={(e) => setKind(e.target.value as TaskKind)} className={FIELD}>
        {KINDS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <input aria-label="task title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Technical screen with the team" className={`w-60 ${FIELD}`} />
      <input aria-label="due" type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} className={FIELD} />
      <select aria-label="reminder" value={remind} onChange={(e) => setRemind(e.target.value)} className={FIELD} disabled={!due}>
        {REMINDERS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <button type="submit" disabled={pending} className="rounded-[var(--radius)] border border-go px-3 py-1.5 text-xs text-go transition-colors hover:bg-go hover:text-paper disabled:opacity-50">
        {pending ? "adding…" : "add task"}
      </button>
      {message ? <span role="status" className="text-xs text-ink-soft">{message}</span> : null}
    </form>
  );
}

function AddContact({ applicationId }: { applicationId: string }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("");
  const [relationship, setRelationship] = useState("recruiter");
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();

  return (
    <form
      className="flex flex-wrap items-end gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        start(async () => {
          const outcome = await addContact(applicationId, { name, email, role, relationship });
          setMessage(outcome.ok ? "added" : outcome.message);
          if (outcome.ok) {
            setName("");
            setEmail("");
            setRole("");
          }
        });
      }}
    >
      <input aria-label="name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" className={`w-40 ${FIELD}`} />
      <input aria-label="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="email (optional)" className={`w-48 ${FIELD}`} />
      <input aria-label="role" value={role} onChange={(e) => setRole(e.target.value)} placeholder="role (optional)" className={`w-36 ${FIELD}`} />
      <select aria-label="relationship" value={relationship} onChange={(e) => setRelationship(e.target.value)} className={FIELD}>
        <option value="recruiter">recruiter</option>
        <option value="hiring_manager">hiring manager</option>
        <option value="interviewer">interviewer</option>
        <option value="referrer">referrer</option>
        <option value="other">other</option>
      </select>
      <button type="submit" disabled={pending} className="rounded-[var(--radius)] border border-rule px-3 py-1.5 text-xs text-ink transition-colors hover:border-go hover:text-go disabled:opacity-50">
        {pending ? "adding…" : "add person"}
      </button>
      {message ? <span role="status" className="text-xs text-ink-soft">{message}</span> : null}
    </form>
  );
}

export function TrackingPanel({
  applicationId,
  tracking,
  calendarUrl,
}: {
  applicationId: string;
  tracking: Tracking;
  calendarUrl: string;
}) {
  const [pending, start] = useTransition();

  return (
    <div className="space-y-10">
      <section aria-labelledby="tasks" className="space-y-4">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h2 id="tasks" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
            Interviews, assessments &amp; follow-ups
          </h2>
          <a href={calendarUrl} className="font-mono text-xs text-ink-soft underline-offset-4 hover:underline">
            add dated tasks to your calendar (.ics)
          </a>
        </div>
        {tracking.tasks.length === 0 ? (
          <p className="text-sm text-ink-faint">No tasks yet.</p>
        ) : (
          <ul className="space-y-3">
            {tracking.tasks.map((task) => (
              <TaskRow key={task.id} applicationId={applicationId} task={task} />
            ))}
          </ul>
        )}
        <AddTask applicationId={applicationId} />
      </section>

      <section aria-labelledby="people" className="space-y-4">
        <h2 id="people" className="font-mono text-xs uppercase tracking-widest text-ink-faint">
          People
        </h2>
        {tracking.contacts.length === 0 ? (
          <p className="text-sm text-ink-faint">Nobody recorded yet.</p>
        ) : (
          <ul className="space-y-2">
            {tracking.contacts.map(({ contact, relationship }) => (
              <li key={contact.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
                <span className="text-ink">{contact.name}</span>
                <span className="font-mono text-xs text-ink-faint">{relationship.replace("_", " ")}</span>
                {contact.role ? <span className="text-ink-soft">{contact.role}</span> : null}
                {contact.email ? <span className="text-ink-soft">{contact.email}</span> : null}
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => start(async () => void (await removeContact(applicationId, contact.id)))}
                  className="ml-auto font-mono text-xs text-ink-faint hover:text-stop disabled:opacity-50"
                >
                  remove
                </button>
              </li>
            ))}
          </ul>
        )}
        <AddContact applicationId={applicationId} />
      </section>
    </div>
  );
}
