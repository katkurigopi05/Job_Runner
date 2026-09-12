"use server";

import { ApiError, api, type PostingAts } from "@/lib/api";

export interface AtsResult {
  ok: boolean;
  message: string;
  report: PostingAts | null;
}

/**
 * Score the owner's résumé against one posting, on demand.
 *
 * On demand rather than for every card in the feed: the score parses the
 * résumé and reads the posting body, and a feed of 200 matches would do that
 * 200 times to answer a question the owner asked about one job.
 *
 * A failure is returned as a message rather than thrown. This is a panel
 * beside a posting, and an unhandled error here would take down the whole
 * feed to report that one score could not be computed.
 */
export async function checkAts(postingId: string, profileId: string): Promise<AtsResult> {
  try {
    return { ok: true, message: "", report: await api.postingAts(postingId, profileId) };
  } catch (error) {
    const message =
      error instanceof ApiError ? error.message : "could not reach the API to score this";
    return { ok: false, message, report: null };
  }
}
