"use server";

import { revalidatePath } from "next/cache";
import { ApiError } from "@/lib/api";

const API = process.env.JOBRUNNER_API ?? "http://127.0.0.1:8000";

export interface SaveResult {
  ok: boolean;
  message: string;
}

/**
 * Save a profile edit.
 *
 * Only fields present in the form are sent. The API distinguishes "not sent"
 * from "sent as null", so an untouched work_auth stays put rather than being
 * cleared — that answer is copied verbatim onto real applications (§2.2) and
 * blanking it by accident is not a cosmetic bug.
 */
export async function saveProfile(
  profileId: string,
  _prev: SaveResult | null,
  form: FormData,
): Promise<SaveResult> {
  const text = (field: string) => {
    const value = form.get(field);
    if (value === null) return undefined;
    const trimmed = String(value).trim();
    return trimmed === "" ? null : trimmed;
  };

  const payload: Record<string, unknown> = {
    label: text("label"),
    phone: text("phone"),
    location: text("location"),
    work_auth: text("work_auth"),
    salary_expectation: text("salary_expectation"),
    needs_sponsorship: form.get("needs_sponsorship") === "on",
    auto_submit: form.get("auto_submit") === "on",
  };

  const score = form.get("min_match_score");
  if (score !== null && String(score).trim() !== "") {
    payload.min_match_score = Number(score);
  }

  // A label is the one field that cannot be cleared — it is how you tell two
  // profiles apart in the queue.
  if (payload.label === null) delete payload.label;

  for (const key of Object.keys(payload)) {
    if (payload[key] === undefined) delete payload[key];
  }

  const response = await fetch(`${API}/profiles/${profileId}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    cache: "no-store",
  }).catch(() => null);

  if (response === null) {
    return { ok: false, message: `Cannot reach the API at ${API}. Start it with \`make api\`.` };
  }

  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = (await response.json()) as { error?: { message: string } };
      if (body.error) message = body.error.message;
    } catch {
      /* keep the status line */
    }
    return { ok: false, message };
  }

  revalidatePath("/profile");
  return { ok: true, message: "Saved." };
}

export { ApiError };
