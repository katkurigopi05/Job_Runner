import { NextResponse, type NextRequest } from "next/server";

import { isLocalName, isLocalOrigin } from "@/lib/local-host";

/** Methods that change something. A server action is a POST. */
const WRITES = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * Refuse a request that names another site, on every path.
 *
 * The API refuses one too, but it cannot see what reached the dashboard:
 * pages render the owner's records here, and server actions call the API
 * from loopback with no `Host` of the browser's attached. So a rebound page
 * (`apps/web/src/lib/local-host.ts`) would read the review queue and approve
 * from it. No `matcher`: a path left out is a path left open.
 */
export function middleware(request: NextRequest): NextResponse {
  const origin = request.headers.get("origin");
  const foreignWrite = WRITES.has(request.method) && origin !== null && !isLocalOrigin(origin);
  if (!isLocalName(request.headers.get("host")) || foreignWrite) {
    return new NextResponse(
      "jobrunner answers to localhost or this machine's address, not to a domain name.",
      { status: 403, headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  }
  return NextResponse.next();
}
