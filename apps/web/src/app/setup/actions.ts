"use server";

import { revalidatePath } from "next/cache";
import { ApiError, api, type RegistrySyncResult } from "@/lib/api";

export type RegistrySyncOutcome =
  | { ok: true; result: RegistrySyncResult }
  | { ok: false; message: string };

/**
 * Preview or apply the registry repair, from the server.
 *
 * A Server Action for the reason every mutation here is one: the API refuses
 * non-loopback callers and has no CORS, so the browser cannot call it. The
 * page always previews first; applying is a second, explicit click.
 */
export async function syncRegistry(dryRun: boolean): Promise<RegistrySyncOutcome> {
  try {
    const result = await api.registrySync(dryRun);
    if (!dryRun) revalidatePath("/setup");
    return { ok: true, result };
  } catch (error) {
    if (error instanceof ApiError) return { ok: false, message: error.message };
    throw error;
  }
}
