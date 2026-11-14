"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api, type Application, type PostingAts } from "@/lib/api";

export interface AtsResult {
  ok: boolean;
  message: string;
  report: PostingAts | null;
}

export interface QueueApplicationResult {
  ok: boolean;
  duplicate?: boolean;
  message: string;
  application: Application | null;
}

/**
 * Queue a matched posting for tailoring and pre-submit form fill.
 *
 * Respects §2.3 (`AUTO_SUBMIT=false`): this only creates a `queued` application
 * so the worker tailors the résumé and parks it in `/review` for approval.
 */
export async function queueApplication(input: {
  candidateId?: string;
  profileId: string;
  url: string;
  ats?: string | null;
}): Promise<QueueApplicationResult> {
  try {
    let candidateId = input.candidateId;
    if (!candidateId) {
      const profiles = await api.profiles();
      const matched = profiles.find((p) => p.id === input.profileId) ?? profiles[0];
      candidateId = matched?.candidate_id;
    }
    if (!candidateId) {
      const candidates = await api.candidates();
      candidateId = candidates[0]?.id;
    }
    if (!candidateId) {
      return {
        ok: false,
        message: "No candidate profile found yet — complete /profile first.",
        application: null,
      };
    }

    const application = await api.createApplication({
      candidate_id: candidateId,
      profile_id: input.profileId,
      url: input.url,
      ats: input.ats ?? null,
    });
    revalidatePath("/matches");
    revalidatePath("/applications");
    revalidatePath("/review");
    return {
      ok: true,
      message: "Queued for tailoring & review",
      application,
    };
  } catch (error) {
    if (error instanceof ApiError) {
      const duplicate = error.code === "duplicate_application" || error.status === 409;
      return {
        ok: false,
        duplicate,
        message: error.message,
        application: null,
      };
    }
    return {
      ok: false,
      message: "could not reach the API to queue this application",
      application: null,
    };
  }
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
