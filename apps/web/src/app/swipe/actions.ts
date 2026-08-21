"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api, type Decision } from "@/lib/api";

export interface DecisionResult {
  ok: boolean;
  message: string;
}

/**
 * Record a swipe, from the server.
 *
 * The deck is a Client Component, so calling `api.decide()` inside it ran the
 * fetch in the *browser* — cross-origin from localhost:3000 to 127.0.0.1:8000,
 * which triggers a CORS preflight the API answers with 405. Every swipe failed
 * and the page reported "cannot reach the API".
 *
 * The fix is not CORS. `lib/api.ts` is explicit that these calls run on the
 * Next server because the API has no authentication and refuses non-loopback
 * callers; opening it to a browser origin would widen exactly the surface that
 * rule protects. A Server Action keeps the request where it was designed to
 * originate, which is also how `/review` already does it.
 */
export async function recordDecision(
  matchId: string,
  decision: Decision,
): Promise<DecisionResult> {
  try {
    await api.decide(matchId, decision);
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }

  // The calibration panel on this page and the counts on the Desk both move
  // with every decision.
  revalidatePath("/swipe");
  revalidatePath("/");
  return { ok: true, message: decision === "interested" ? "kept" : "skipped" };
}
