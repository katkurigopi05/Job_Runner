"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api, type ChecklistItem, type TaskKind } from "@/lib/api";

export type TrackingResult = { ok: true } | { ok: false; message: string };

async function run(applicationId: string, work: () => Promise<unknown>): Promise<TrackingResult> {
  try {
    await work();
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }
  revalidatePath(`/applications/${applicationId}`);
  revalidatePath("/tracker");
  return { ok: true };
}

/** Record a person on this application. Nothing is sent to them. */
export async function addContact(
  applicationId: string,
  input: { name: string; email: string; role: string; relationship: string },
): Promise<TrackingResult> {
  if (!input.name.trim()) return { ok: false, message: "a name is required" };
  return run(applicationId, () =>
    api.linkContact(applicationId, {
      relationship: input.relationship,
      contact: {
        name: input.name.trim(),
        email: input.email.trim() || null,
        role: input.role.trim() || null,
      },
    }),
  );
}

export async function removeContact(applicationId: string, contactId: string): Promise<TrackingResult> {
  return run(applicationId, () => api.unlinkContact(applicationId, contactId));
}

export async function addTask(
  applicationId: string,
  input: { kind: TaskKind; title: string; dueAt: string | null; remindMinutesBefore: number | null },
): Promise<TrackingResult> {
  if (!input.title.trim()) return { ok: false, message: "a title is required" };
  const due = input.dueAt ? new Date(input.dueAt) : null;
  if (due && Number.isNaN(due.getTime())) return { ok: false, message: "that date is not valid" };
  const reminder =
    due && input.remindMinutesBefore !== null
      ? new Date(due.getTime() - input.remindMinutesBefore * 60_000).toISOString()
      : null;
  return run(applicationId, () =>
    api.createTask(applicationId, {
      kind: input.kind,
      title: input.title.trim(),
      due_at: due ? due.toISOString() : null,
      reminder_at: reminder,
    }),
  );
}

export async function updateTask(
  applicationId: string,
  taskId: string,
  patch: { checklist?: ChecklistItem[]; completed?: boolean },
): Promise<TrackingResult> {
  return run(applicationId, () => api.updateTask(taskId, patch));
}

export async function deleteTask(applicationId: string, taskId: string): Promise<TrackingResult> {
  return run(applicationId, () => api.deleteTask(taskId));
}
