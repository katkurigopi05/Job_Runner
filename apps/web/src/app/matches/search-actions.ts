"use server";

import { ApiError, api } from "@/lib/api";

export type SaveSearchResult = { ok: true } | { ok: false; message: string };

/**
 * Save the current feed filters as the default search.
 *
 * Stored in `search_preferences`, not on the profile: a saved "at least
 * $150k" must never be one edit away from a salary answer on a real form.
 */
export async function saveDefaultSearch(filters: Record<string, string>): Promise<SaveSearchResult> {
  try {
    await api.saveSearchPreference("default", filters);
    return { ok: true };
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }
}
