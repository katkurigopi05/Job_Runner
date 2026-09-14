"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api } from "@/lib/api";

export type SplitResult = { ok: true } | { ok: false; message: string };

/**
 * Take one listing out of its requisition group, permanently.
 *
 * The owner's correction of a merge. The grouping pass never re-attaches a
 * split listing, so the next crawl does not undo it.
 */
export async function splitListing(postingId: string, viewing: string): Promise<SplitResult> {
  try {
    await api.splitPosting(postingId);
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }
  revalidatePath(`/postings/${viewing}`);
  return { ok: true };
}
