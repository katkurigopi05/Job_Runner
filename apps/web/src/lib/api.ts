/**
 * Server-side client for the FastAPI app.
 *
 * Everything here runs on the Next server, never in the browser. That is not
 * an optimization: the API has no authentication and refuses non-loopback
 * callers, so the request has to originate from this process over loopback.
 */

const API = process.env.JOBRUNNER_API ?? "http://127.0.0.1:8000";

export type ApplicationStatus =
  | "queued"
  | "running"
  | "needs_review"
  | "needs_otp"
  | "submitted"
  | "failed";

export type FailureReason =
  | "job_closed"
  | "unsupported_site"
  | "incomplete_candidate"
  | "manual_completion_required"
  | "rejected_at_review"
  | "site_error";

/** One question the agent could not answer. The text is the employer's. */
export interface UnansweredQuestion {
  key?: string;
  question: string;
  kind?: string;
  required?: boolean;
  options?: string[] | null;
}

export interface FilledField {
  key?: string;
  question?: string;
  value?: unknown;
}

export interface ReviewRecord {
  fill_rate?: number;
  filled?: FilledField[];
  skipped?: FilledField[];
  unanswered?: UnansweredQuestion[];
  screenshot_ref?: string | null;
  owner_answers?: Record<string, unknown>;
  owner_approved?: boolean;
  reason?: string;
  questions?: string[];
  score?: number | null;
  min_match_score?: number;
}

export interface Application {
  id: string;
  candidate_id: string;
  profile_id: string;
  url: string;
  ats: string | null;
  status: ApplicationStatus;
  failure_reason: FailureReason | null;
  review: ReviewRecord | null;
  created_at: string;
  updated_at: string;
}

export interface ApplicationEvent {
  id: string;
  application_id: string;
  type: string;
  payload: Record<string, unknown> | null;
  at: string;
}

export interface Profile {
  id: string;
  candidate_id: string;
  label: string;
  base_resume_id: string | null;
  phone: string | null;
  location: string | null;
  work_auth: string | null;
  needs_sponsorship: boolean | null;
  salary_expectation: string | null;
  min_match_score: number;
  auto_submit: boolean;
}

export interface Candidate {
  id: string;
  name: string;
  email: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...init?.headers },
      cache: "no-store",
    });
  } catch (cause) {
    // The API not running is the single most likely failure on a local tool,
    // and "fetch failed" tells the owner nothing about how to fix it.
    throw new ApiError(
      503,
      "api_unreachable",
      `Cannot reach the jobrunner API at ${API}. Start it with \`make api\`.`,
    );
  }

  if (!response.ok) {
    let code = "internal_error";
    let message = response.statusText;
    try {
      const body = (await response.json()) as { error?: { code: string; message: string } };
      if (body.error) {
        code = body.error.code;
        message = body.error.message;
      }
    } catch {
      /* non-JSON error body; the status line is all there is */
    }
    throw new ApiError(response.status, code, message);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  applications: () => request<Application[]>("/applications"),
  application: (id: string) => request<Application>(`/applications/${id}`),
  events: (id: string) => request<ApplicationEvent[]>(`/applications/${id}/events`),
  candidates: () => request<Candidate[]>("/candidates"),
  profiles: () => request<Profile[]>("/profiles"),

  review: (id: string, body: { approve: boolean; answers?: Record<string, unknown>; note?: string }) =>
    request<Application>(`/applications/${id}/review`, {
      method: "POST",
      body: JSON.stringify({ answers: {}, ...body }),
    }),

  otp: (id: string, code: string) =>
    request<Application>(`/applications/${id}/otp`, {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
};

/** Statuses that are waiting on the owner rather than on the machine. */
export const NEEDS_OWNER: ApplicationStatus[] = ["needs_review", "needs_otp"];

export function isTerminal(status: ApplicationStatus): boolean {
  return status === "submitted" || status === "failed";
}
