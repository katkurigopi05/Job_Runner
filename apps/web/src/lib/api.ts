/**
 * Server-side client for the FastAPI app.
 *
 * Everything here runs on the Next server, never in the browser. That is not
 * an optimization: the API has no authentication and refuses non-loopback
 * callers, so the request has to originate from this process over loopback.
 */

const API = process.env.JOBRUNNER_API ?? "http://127.0.0.1:8000";

/**
 * Where the API lives, for the one case the browser must call it directly.
 *
 * Downloads are that case: streaming a PDF through the Next server to hand
 * it back unchanged would buy nothing, and the browser is already on
 * loopback so the API accepts it.
 */
export const API_BASE = API;

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

export interface ResumeChange {
  original: string;
  tailored: string;
  inline_html?: string;
}

/** What tailoring changed, shown before the owner approves. §2.1. */
export interface ResumeDiff {
  changed: number;
  unchanged: number;
  /** Rewrites the fabrication guard refused and replaced with the original. */
  rejected: number;
  unified: string;
  changes: ResumeChange[];
}

export interface ReviewRecord {
  fill_rate?: number;
  filled?: FilledField[];
  skipped?: FilledField[];
  unanswered?: UnansweredQuestion[];
  screenshot_ref?: string | null;
  resume_diff?: ResumeDiff | null;
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
  /** The tailored résumé to be attached. Null means the profile's base goes as-is. */
  tailored_resume_id: string | null;
  /**
   * What the employer's reply said, once one arrived. Distinct from `status`,
   * which tracks our side: an application is `submitted` the moment it is sent
   * and stays there whether the answer is an offer or silence.
   */
  outcome: string | null;
  outcome_at: string | null;
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

export interface Resume {
  id: string;
  candidate_id: string;
  version: number;
  storage_ref: string;
  is_default: boolean;
  created_at: string;
}

/** What the parser pulled out, so it can be checked before it is trusted. */
export interface ResumeParsed {
  id: string;
  version: number;
  contact: Record<string, unknown>;
  /** Section name to line count. A missing section here is a warning sign. */
  sections: Record<string, number>;
  line_count: number;
  parsed: {
    contact?: Record<string, unknown>;
    preamble?: string[];
    sections?: Record<string, string[]>;
    raw_lines?: string[];
  };
}

export type Classification =
  | "interview"
  | "rejection"
  | "offer"
  | "info_request"
  | "acknowledgement"
  | "otp"
  | "noise";

/** A recruiter reply, as received. Subject and body are the sender's words. */
export interface InboundMessage {
  id: string;
  candidate_id: string;
  application_id: string | null;
  from_addr: string;
  subject: string | null;
  body: string | null;
  classification: Classification | null;
  at: string;
}

/** A scored posting, with the breakdown that produced the score. */
export interface Match {
  id: string;
  profile_id: string;
  posting_id: string;
  score: number;
  title: string | null;
  location: string | null;
  url: string;
  ats_type: string | null;
  first_seen_at: string;
  /** When the source says it went up. Null when the board does not say. */
  published_at: string | null;
  /** Hours from publication to us noticing. Null means unmeasurable, not zero. */
  lag_hours: number | null;
  closed: boolean;
  title_similarity: number;
  body_similarity: number;
  /** Hard filters that ruled it out — location, seniority, sponsorship. */
  excluded_by: string[];
}

export interface Candidate {
  id: string;
  name: string;
  email: string;
}

/** The posting, as much of it as the handoff screen shows. */
export interface PacketPosting {
  title: string | null;
  company: string | null;
  location: string | null;
  url: string | null;
  description: string | null;
}

/** The file to upload, and whether tailoring actually produced it. */
export interface PacketResume {
  resume_id: string;
  download_path: string;
  is_tailored: boolean;
  rewritten_bullets: number;
  rejected_rewrites: number;
}

export interface PacketAnswer {
  question: string;
  value: string;
}

export interface PacketQuestion {
  question: string;
  kind: string | null;
  required: boolean;
}

/**
 * Everything needed to finish one application by hand.
 *
 * Exists because the run stops at the captcha every supported ATS mounts on
 * the apply form. The work up to that point is real, and this is how it gets
 * handed over instead of thrown away.
 */
export interface ApplicationPacket {
  application_id: string;
  status: ApplicationStatus;
  failure_reason: FailureReason | null;
  ats: string | null;
  apply_url: string;
  posting: PacketPosting | null;
  resume: PacketResume | null;
  answers: PacketAnswer[];
  unanswered: PacketQuestion[];
  screenshot_path: string | null;
  ready_to_submit: boolean;
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
  packet: (id: string) => request<ApplicationPacket>(`/applications/${id}/packet`),
  candidates: () => request<Candidate[]>("/candidates"),
  // Scoped to a candidate by the API, not optional. Single-user or not,
  // the route requires it.
  resumes: (candidateId: string) =>
    request<Resume[]>(`/resumes?candidate_id=${encodeURIComponent(candidateId)}`),
  resumeParsed: (id: string) => request<ResumeParsed>(`/resumes/${id}/parsed`),
  profiles: () => request<Profile[]>("/profiles"),
  inbox: () => request<InboundMessage[]>("/inbox"),
  matches: (includeApplied = false) =>
    request<Match[]>(`/matches?include_applied=${includeApplied}`),
  /** Filters are the owner's search, passed straight through as query params. */
  matchesFiltered: (query: URLSearchParams) => request<Match[]>(`/matches?${query}`),
  unrouted: () => request<InboundMessage[]>("/inbox/unrouted"),

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
