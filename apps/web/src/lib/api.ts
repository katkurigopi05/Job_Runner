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
export interface RecruiterChange {
  before: number;
  after: number;
  shortlist_before: string;
  shortlist_after: string;
  scan_after: number;
  qualification_after: number;
  credibility_after: number;
  technical_after: number;
  findings: string[];
}

export interface ResumeDiff {
  /**
   * Optional because the reuse paths genuinely have none of it. When an
   * overnight batch or the tailoring cache already wrote this document, the
   * apply run attaches it without rewriting anything, and the payload is a
   * `reused` marker and the model that wrote it. The old required typing said
   * otherwise and `ResumeDiffView` believed it, mapping over an undefined
   * `changes`.
   */
  changed?: number;
  unchanged?: number;
  /** Rewrites the fabrication guard refused and replaced with the original. */
  rejected?: number;
  /**
   * Bullets the model never answered — a transport error, an empty completion,
   * a spent allowance.
   *
   * Apart from `rejected` deliberately. One says what the model tried to write,
   * the other says the network failed, and adding them together makes a
   * provider that was down look like one that kept trying to invent.
   */
  provider_failures?: number;
  /**
   * How an ATS reads the résumé, before and after this run, against this
   * posting. Absent when the run had no posting text to score against, and on
   * every diff written before the field existed.
   *
   * Both halves are carried rather than one combined number. They fail
   * independently: a run that raises keyword coverage while lowering the parse
   * score has made the document worse in the way that matters most, because a
   * résumé an ATS cannot segment is a row of empty columns.
   */
  ats?: {
    parse_before: number;
    parse_after: number;
    keywords_before: number;
    keywords_after: number;
    /** Posting terms the tailored résumé now matches and the source did not. */
    gained: string[];
    /**
     * Terms the posting asks for that the résumé still cannot back. Not a
     * to-do list — writing one in would be fabrication under §2.1.
     */
    still_missing: string[];
  } | null;
  /**
   * What a person is likely to make of the résumé, before and after.
   *
   * Beside `ats` rather than folded into it: they answer different questions
   * and can move in opposite directions. A rewrite that packs the posting's
   * vocabulary into every bullet raises keyword coverage and lowers this, and
   * one number would hide the trade the owner most needs to see.
   *
   * Absent on every diff written before the field existed.
   */
  recruiter?: RecruiterChange | null;
  unified?: string;
  changes?: ResumeChange[];
  /** This run attached an already-tailored résumé rather than writing one. */
  reused?: boolean;
  /**
   * The owner fixed the document after tailoring wrote it — `owner_edit` for a
   * hand edit, `comparison` for a pick from the model comparison.
   *
   * Set means the changes below describe the résumé this one was *derived
   * from*, not the file that will be uploaded. Rendering the diff without
   * saying so would repeat the §15 failure in miniature: a review screen
   * describing a document other than the one being sent.
   */
  owner_pinned?: string | null;
  /**
   * Which model wrote the document: "gemini", or "ollama:llama3.1" when §7's
   * fallback answered after the remote allowance ran out. Null or absent means
   * unrecorded — a résumé tailored before the column existed — never a guess.
   */
  answered_by?: string | null;
}

/**
 * One model's attempt at the same posting, for the comparison view.
 *
 * `requested` and `answered_by` are separate because §7's fallback answers with
 * the local model when the remote allowance is spent — a column labelled by
 * what was asked for would compare the local model against itself.
 *
 * `error` set means this side could not run: no key, spent quota, provider
 * unreachable. It is rendered rather than dropped; a comparison missing half of
 * itself reads as a verdict on the half that is there.
 */
export interface TailoringCandidate {
  requested: string;
  answered_by?: string | null;
  resume_id?: string | null;
  changed: number;
  unchanged: number;
  rejected: number;
  /** Bullets the model never answered. Never folded into `rejected`. */
  provider_failures?: number;
  unified?: string;
  changes?: ResumeChange[];
  reused?: boolean;
  error?: string | null;
}

/**
 * The letter written for this application, if the form asked for one.
 *
 * `accepted: false` with a `rejected_reason` is a real outcome, not an absence:
 * a letter has no original to fall back to, so the alternative to a bad one is
 * none — and "the guard refused it" has to look different from "never tried".
 */
export interface CoverLetter {
  accepted: boolean;
  rejected_reason?: string | null;
  word_count?: number;
  entities_checked?: number;
  /** Sentences the guard stripped. A letter that survived only by deletion is worth seeing. */
  sentences_dropped?: number;
  /** Which model wrote it — §7's fallback applies here as it does to tailoring. */
  answered_by?: string | null;
  ref?: string | null;
  text?: string | null;
  reused?: boolean;
}

export interface BaseResumeChoice {
  resume_id: string;
  version: number;
  score: number;
  reason: string;
  considered?: { version: number; score: number }[];
}

export interface ReviewRecord {
  fill_rate?: number;
  filled?: FilledField[];
  skipped?: FilledField[];
  unanswered?: UnansweredQuestion[];
  screenshot_ref?: string | null;
  resume_diff?: ResumeDiff | null;
  /** Which of the owner's résumés this application tailored from, and why. */
  base_resume?: BaseResumeChoice | null;
  /** Present once the owner has asked for a local-vs-cloud comparison. */
  tailoring_comparison?: TailoringCandidate[] | null;
  /** Null when the form never asked for a letter, which most do not. */
  cover_letter?: CoverLetter | null;
  owner_answers?: Record<string, unknown>;
  owner_approved?: boolean;
  reason?: string;
  questions?: string[];
  score?: number | null;
  min_match_score?: number;
  screening?: Screening | null;
  /** §25/§44/§53 — the composite, its band, and what is stopping this going. */
  readiness?: Readiness | null;
}

/** One measured axis of readiness. `measured` false means its input was
 *  missing, so it carries no weight and its `finding` says why. */
export interface ReadinessComponent {
  name: string;
  score: number;
  weight: number;
  finding: string;
  measured: boolean;
}

export interface ReadinessBlocker {
  code: string;
  detail: string;
}

/**
 * `score` and `tier` are null when too little was measurable to say — which is
 * a different answer from a low score and is rendered as one. `ready` is false
 * on any blocker regardless of `score`: the gate is a fact about whether the
 * application can be completed, the score is an opinion about how it would land.
 */
export interface Readiness {
  score: number | null;
  tier: string | null;
  ready: boolean;
  components: ReadinessComponent[];
  blockers: ReadinessBlocker[];
  weakest: string | null;
  summary: string;
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
  /**
   * Current work authorization — a *filter*, never typed onto a form.
   * `work_auth` above is the field §2.2 copies verbatim; this one only decides
   * which postings are shown, and null means unstated, which filters nothing.
   */
  citizenship_status:
    | "us_citizen"
    | "permanent_resident"
    | "other_authorized"
    | "not_authorized"
    | null;
  salary_expectation: string | null;
  min_match_score: number;
  auto_submit: boolean;
  /**
   * The rung being applied at. A *filter*: null means "do not filter on
   * level", which is the shipped default.
   */
  target_seniority: string | null;
  /**
   * The most years a posting may demand and still be shown — the owner's
   * bound, not a count of their experience (§1). null filters nothing, and
   * only a demand the posting states as mandatory is ever excluded on.
   */
  max_required_experience_years: number | null;
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
  /**
   * "alias" means the +app tag we applied with came back in a header — an
   * exact key. "inferred" means it was matched on sender and content, which
   * is a guess: those never move an application's outcome.
   */
  link_method: "alias" | "inferred" | "unlinked";
  link_confidence: number | null;
  at: string;
}

/** A scored posting, with the breakdown that produced the score. */
/** A week's activity, composed from the funnel and cadence reports. */
export interface Digest {
  window_days: number;
  postings_seen: number;
  applications_created: number;
  applications_submitted: number;
  replies_received: number;
  awaiting_review: number;
  follow_ups_due: number;
  /** Named rather than left as six zeroes: a quiet week usually means the
   *  crawler stopped, not that the market did. */
  quiet_week: boolean;
}

/** Counts, not a page — `GET /matches` caps at 200. */
export interface MatchSummary {
  total: number;
  undecided: number;
  interested: number;
}

export interface PostingSearch {
  results: { id: string; title: string | null; ats_type: string | null }[];
  total?: number;
}

export type Decision = "interested" | "skipped";

/** What the owner's swipes say the score threshold should be. */
/**
 * One posting offered for grading, with why it was offered.
 *
 * `stream` is the audit trail for sampling bias. A corpus that turns out to be
 * all `uncertain` was drawn from the ranker's own shortlist and carries the
 * weakness `provenance: owner` is meant to have escaped — see
 * packages/matching/active.py.
 */
export interface LabelCandidate {
  posting_id: string;
  title: string | null;
  location: string | null;
  url: string;
  description: string | null;
  first_seen_at: string;
  stream: "uncertain" | "unseen" | "confident";
  /** null means the ranker never scored it. Not the same as zero. */
  score: number | null;
}

export interface LabelSummary {
  profile_id: string;
  profile: string;
  total: number;
  by_grade: Record<string, number>;
  by_stream: Record<string, number>;
  target: number;
  remaining: number;
  usable: boolean;
  notes: string[];
}

export interface Calibration {
  decided: number;
  interested: number;
  skipped: number;
  interested_mean: number | null;
  skipped_mean: number | null;
  separation: number | null;
  suggested_min_score: number | null;
  enough_data: boolean;
}

export interface Match {
  id: string;
  profile_id: string;
  posting_id: string;
  score: number;
  /** `interested`, `skipped`, or null for not yet seen. A verdict on the
   *  posting — never an instruction to apply. */
  decision: Decision | null;
  decided_at: string | null;
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
  /** Terms the posting emphasizes and the profile evidences. */
  matched_terms: string[];
  /**
   * What the posting wants that your résumé does not show. The tailorer is
   * forbidden from inventing these, so this is where you decide whether one
   * is true of you and worth writing in.
   */
  missing_terms: string[];
  legitimacy: Legitimacy | null;
  rubric: Rubric | null;
  /** Hard filters that ruled it out — location, seniority, sponsorship. */
  excluded_by: string[];
  /**
   * What the posting says about work authorization, with its own wording as
   * evidence. Always present — `summary` reads "Unknown — verify with
   * employer" when the posting said nothing, because a blank line here would
   * read as "no restrictions", which is a claim no posting made.
   */
  eligibility: Eligibility;
  /**
   * Years of experience the posting demands, and how firmly it asks.
   *
   * `null` when the posting states none, which is most of them. Unlike the
   * authorization line, a silence here is genuinely the absence of a
   * requirement rather than an unanswered question, so it renders as nothing.
   */
  experience: PostingExperience | null;
  /** Pay as the posting states it. Null means not stated — never zero. */
  compensation: Compensation | null;
  /** Skills and education with the line each was read from. Null until extracted. */
  requirements: PostingRequirements | null;
  /** Every listing of this requisition when grouped; empty when it stands alone. */
  sources: MatchSource[];
  /** Base score adjusted by explicit ranking preferences; null when there are none. */
  personalized_score: number | null;
  adjustments: RankingAdjustment[];
}

export interface RankingAdjustment {
  kind: string;
  value: string;
  weight: number;
  why: string;
}

export type RankingKind = "company" | "skill" | "title_term" | "location_term" | "remote";

export interface RankingPreference {
  id: string;
  scope: string;
  kind: RankingKind;
  value: string;
  weight: number;
  source: "explicit" | "suggestion";
  note: string | null;
  updated_at: string;
}

export interface RankingSuggestion {
  kind: RankingKind;
  value: string;
  weight: number;
  evidence: string;
}

export interface RankingEvaluation {
  owner_labels: number;
  streams: Record<string, number>;
  held_out: number;
  preferences: number;
  status: "insufficient_labels" | "evaluated" | string;
  message: string;
  k: number;
  learned_model: string;
  base_ndcg: number | null;
  base_interval: number[] | null;
  personalized_ndcg: number | null;
  personalized_interval: number[] | null;
  promotable: boolean;
  blockers: string[];
}

export const SKIP_REASONS = [
  { value: "salary", label: "pay" },
  { value: "location", label: "location" },
  { value: "seniority", label: "level" },
  { value: "skills", label: "skills" },
  { value: "requirements", label: "requirements" },
  { value: "company", label: "company" },
  { value: "role", label: "kind of role" },
  { value: "duplicate", label: "duplicate" },
  { value: "other", label: "other" },
] as const;

export interface MatchSource {
  posting_id: string;
  url: string;
  source: string;
  closed: boolean;
  decision: string | null;
}

export interface Compensation {
  minimum: number | null;
  maximum: number | null;
  currency: string | null;
  /** Null when the posting did not say — not assumed to be annual. */
  period: string | null;
  quote: string | null;
}

export interface SkillEvidence {
  skill: string;
  label: string;
  quote: string;
}

export interface EducationRequirement {
  level: "high_school" | "associate" | "bachelor" | "master" | "phd";
  requirement: "required" | "preferred" | "unclassified";
  equivalent_experience: boolean;
  quote: string;
}

export interface PostingRequirements {
  version: number;
  compensation: Compensation | null;
  skills: {
    required: SkillEvidence[];
    preferred: SkillEvidence[];
    /** Named, but under nothing that says whether it is asked for. */
    unclassified: SkillEvidence[];
  };
  education: EducationRequirement | null;
  /** What the posting did not state: compensation, pay_period, skills, education. */
  unknown: string[];
}

export interface VersionChange {
  field: string;
  before: string | string[] | null;
  after: string | string[] | null;
}

export interface PostingVersionEntry {
  version: number;
  captured_at: string;
  pay: string;
  changes: VersionChange[];
}

/** One listing of a requisition. Grouping never hides a source. */
export interface PostingSource {
  posting_id: string;
  url: string;
  ats_type: string | null;
  source: string;
  title: string | null;
  location: string | null;
  closed: boolean;
  decisions: string[];
  application_statuses: string[];
  evidence: string | null;
}

export interface PostingHistory {
  posting_id: string;
  title: string | null;
  canonical_job_id: string | null;
  locked: boolean;
  sources: PostingSource[];
  versions: PostingVersionEntry[];
}

/** A saved feed search. Filters only — never read by anything that applies. */
export interface SearchPreference {
  id: string;
  scope: string;
  name: string;
  filters: Record<string, string>;
  updated_at: string;
}

/** One years requirement and the line it was read from. */
export interface YearsDemand {
  years: number;
  /**
   * `mandatory` is the only kind that excludes. `preferred` means the posting
   * itself said the requirement was optional, and `ambiguous` means it sat
   * under no heading that said either way — neither is enough for a hard
   * filter, so both are shown rather than acted on.
   */
  demand: "mandatory" | "preferred" | "ambiguous";
  quote: string;
}

export interface PostingExperience {
  stated: boolean;
  /** The smallest number of years the posting insists on, if it insists. */
  mandatory_minimum: number | null;
  preferred_minimum: number | null;
  summary: string | null;
  demands: YearsDemand[];
}

/**
 * A posting's authorization statements, never an inference from their absence.
 *
 * `unstated` means the posting is silent and `ambiguous` means it said
 * something that does not resolve ("with or without sponsorship"). Neither is
 * permission, and nothing here implies OPT or STEM-OPT acceptance, E-Verify
 * participation, or a history of sponsoring — none of those follow from
 * silence.
 */
export interface Eligibility {
  sponsorship: "available" | "unavailable" | "ambiguous" | "unstated";
  citizenship: "citizens_only" | "citizens_or_residents_only" | "unstated";
  /** Whether the posting resolved either question at all. */
  certain: boolean;
  summary: string;
  /** The posting's own sentences behind each claim. */
  evidence: { claim: string; quote: string }[];
}

/** Whether the posting looks real and open. Never folded into the score. */
export interface Legitimacy {
  tier: "high_confidence" | "caution" | "suspicious";
  signals: LegitimacySignal[];
  /** True of real postings too — contract wording, a benefits mismatch. */
  advisories: LegitimacySignal[];
}

export interface LegitimacySignal {
  name: string;
  weight: "positive" | "neutral" | "concerning";
  finding: string;
}

/** The score broken down. Explains the ranking; does not produce it. */
export interface Rubric {
  overall: number;
  dimensions: RubricDimension[];
  /** The dimension dragging it down — the one worth reading first. */
  weakest: string | null;
}

export interface RubricDimension {
  name: string;
  score: number;
  weight: number;
  finding: string;
}

/** Questions read off the form before anything was answered. */
export interface Screening {
  knock_outs: ScreenedQuestion[];
  cautions: ScreenedQuestion[];
}

export interface ScreenedQuestion {
  key: string;
  label: string;
  reason: string;
  finding: "knock_out" | "caution";
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

export interface AtsFinding {
  code: string;
  detail: string;
  cost: number;
  line: string | null;
}

/**
 * §23's "before" score for one posting, computed on request.
 *
 * `scored_against_posting` false means the posting carried no description, in
 * which case `keywords` must not be shown — 0 there means "not asked", not
 * "matches nothing".
 */
export interface PostingAts {
  posting_id: string;
  resume_id: string;
  resume_version: number;
  resume_reason: string;
  parse: number;
  keywords: number;
  scored_against_posting: boolean;
  supported: string[];
  missing: string[];
  findings: AtsFinding[];
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
  } catch {
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
      const body = (await response.json()) as {
        error?: { code: string; message: string };
      };
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

/**
 * What `/health` reports. `status` is `degraded` when the API answers but
 * cannot reach Postgres — the case a hardcoded "ok" used to hide.
 */
export interface Health {
  status: "ok" | "degraded";
  api: string;
  database: "ok" | "down";
}

export type SetupState = "ok" | "attention" | "blocked" | "unknown";

/** One thing the installation needs, whether it has it, and how to get it. */
export interface SetupItem {
  key: string;
  title: string;
  group: "core" | "credentials" | "discovery" | "tools" | string;
  state: SetupState;
  detail: string;
  /** Commands or edits, in the order to try them. */
  steps: string[];
  /** Non-secret facts behind the verdict. */
  facts: Record<string, string | number | boolean | null>;
  actions: string[];
}

export interface SetupStatus {
  generated_at: string;
  overall: SetupState;
  items: SetupItem[];
}

export interface RegistrySyncResult {
  dry_run: boolean;
  summary: string;
  created: number;
  verified: number;
  newer_in_db: number;
  retired: number;
  retired_newer_in_db: number;
  moved_boards: string[];
}

/** Whether the crawler is working, waiting, or stuck waiting for a worker. */
export interface CrawlStatus {
  running: boolean;
  pending: number;
  /** Work queued with nobody holding it — `make worker` is not up. */
  stalled: boolean;
  last_finished_at?: string | null;
  last_status?: string | null;
  /** Newest posting anyone has seen. This is what staleness actually means. */
  newest_posting_at?: string | null;
}

/**
 * Where the registry has got to. `CrawlStatus` answers "is a crawl running";
 * this answers "did my companies get anywhere", which has several quite
 * different causes when the feed is empty.
 */
export interface DiscoveryStatus {
  companies_total: number;
  pending: number;
  unverified: number;
  verified: number;
  retrying: number;
  retrying_next_at?: string | null;
  needs_review: number;
  discovery_queue: number;
  fetch_queue: number;
  postings_total: number;
  postings_open: number;
  postings_unembedded: number;
  matches_total: number;
  corpus_documents: number;
  corpus_min_documents: number;
  weighting: string;
  weighting_reason: string;
}

export const api = {
  health: () => request<Health>("/health"),
  crawlStatus: () => request<CrawlStatus>("/crawl/status"),
  discoveryStatus: () => request<DiscoveryStatus>("/companies/status"),
  setupStatus: () => request<SetupStatus>("/setup/status"),
  registrySync: (dryRun: boolean) =>
    request<RegistrySyncResult>("/setup/registry-sync", {
      method: "POST",
      body: JSON.stringify({ dry_run: dryRun }),
    }),

  applications: () => request<Application[]>("/applications"),
  application: (id: string) => request<Application>(`/applications/${id}`),
  events: (id: string) =>
    request<ApplicationEvent[]>(`/applications/${id}/events`),
  packet: (id: string) =>
    request<ApplicationPacket>(`/applications/${id}/packet`),
  manualQueue: (limit = 25) =>
    request<ApplicationPacket[]>(`/applications/queue/manual?limit=${limit}`),
  markSubmitted: (id: string, note?: string) =>
    request<Application>(`/applications/${id}/submitted`, {
      method: "POST",
      body: JSON.stringify({ note: note ?? null }),
    }),
  candidates: () => request<Candidate[]>("/candidates"),
  // Scoped to a candidate by the API, not optional. Single-user or not,
  // the route requires it.
  resumes: (candidateId: string) =>
    request<Resume[]>(
      `/resumes?candidate_id=${encodeURIComponent(candidateId)}`,
    ),
  resumeParsed: (id: string) => request<ResumeParsed>(`/resumes/${id}/parsed`),

  /** How an ATS reads this profile's résumé against one posting. */
  postingAts: (postingId: string, profileId: string) =>
    request<PostingAts>(
      `/postings/${postingId}/ats?profile_id=${encodeURIComponent(profileId)}`,
    ),

  /**
   * Save an edited résumé. Creates a new version rather than rewriting the one
   * an application may already have sent, re-renders the PDF so the file and
   * the parsed form cannot disagree, and (by default) moves every profile that
   * used the source onto it.
   */
  editResume: (
    id: string,
    body: {
      contact: {
        name?: string;
        email?: string;
        phone?: string;
        links?: string[];
      };
      sections: Record<string, string[]>;
      adopt?: boolean;
    },
  ) =>
    request<Resume>(`/resumes/${id}/edit`, {
      method: "POST",
      body: JSON.stringify({ adopt: true, ...body }),
    }),
  profiles: () => request<Profile[]>("/profiles"),
  inbox: () => request<InboundMessage[]>("/inbox"),
  matches: (includeApplied = false) =>
    request<Match[]>(`/matches?include_applied=${includeApplied}`),
  /** Filters are the owner's search, passed straight through as query params. */
  matchesFiltered: (query: URLSearchParams) =>
    request<Match[]>(`/matches?${query}`),
  calibration: () => request<Calibration>("/matches/calibration"),
  decideWithReason: (matchId: string, decision: Decision, reason?: string, note?: string) =>
    request<{ id: string; decision: Decision | null; skip_reason: string | null }>(
      `/matches/${matchId}/decision`,
      {
        method: "POST",
        body: JSON.stringify({ decision, reason: reason ?? null, note: note ?? null }),
      },
    ),
  rankingPreferences: () => request<RankingPreference[]>("/ranking/preferences"),
  saveRankingPreference: (body: {
    kind: RankingKind;
    value: string;
    weight: number;
    source?: "explicit" | "suggestion";
    note?: string | null;
  }) =>
    request<RankingPreference>("/ranking/preferences", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteRankingPreference: (id: string) =>
    request<void>(`/ranking/preferences/${id}`, { method: "DELETE" }),
  rankingSuggestions: () => request<RankingSuggestion[]>("/ranking/suggestions"),
  rankingEvaluation: () => request<RankingEvaluation>("/ranking/evaluation"),
  postingHistory: (id: string) => request<PostingHistory>(`/postings/${id}/history`),
  splitPosting: (id: string) =>
    request<PostingHistory>(`/postings/${id}/split`, { method: "POST" }),
  searchPreference: (name: string) =>
    request<SearchPreference>(`/search-preferences/${encodeURIComponent(name)}`),
  saveSearchPreference: (name: string, filters: Record<string, string>) =>
    request<SearchPreference>(`/search-preferences/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ filters }),
    }),
  /**
   * `profileId` is optional because the route is: `/labels/next` picks the
   * only profile when there is one, and refuses to guess when there are
   * several. The screen has to be able to say which.
   */
  labelQueue: (size: number, profileId?: string) =>
    request<LabelCandidate[]>(
      `/labels/next?size=${size}${profileId ? `&profile_id=${profileId}` : ""}`,
    ),
  labelSummary: (profileId?: string) =>
    request<LabelSummary>(
      `/labels/summary${profileId ? `?profile_id=${profileId}` : ""}`,
    ),
  /**
   * `servedStream` is a hint the server may only use to *weaken* the recorded
   * stream, never to strengthen it — it cannot be used to claim `unseen`.
   */
  /**
   * `profileId` matters for the same reason it does on the queue: `POST
   * /labels` refuses to guess when several profiles exist. Scoping only the
   * read left the screen rendering and every grade rejected on submit.
   */
  recordLabel: (
    postingId: string,
    relevance: number,
    servedStream?: string,
    note?: string,
    profileId?: string,
  ) =>
    request<{ id: string; posting_id: string; relevance: number }>("/labels", {
      method: "POST",
      body: JSON.stringify({
        posting_id: postingId,
        relevance,
        note: note ?? null,
        served_stream: servedStream ?? null,
        profile_id: profileId ?? null,
      }),
    }),
  digest: () => request<Digest>("/analytics/digest"),
  matchSummary: () => request<MatchSummary>("/matches/summary"),
  /** Returns a confirmation, not a full Match — the handler has no posting. */
  decide: (matchId: string, decision: Decision) =>
    request<{
      id: string;
      decision: Decision | null;
      decided_at: string | null;
    }>(`/matches/${matchId}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    }),
  unrouted: () => request<InboundMessage[]>("/inbox/unrouted"),

  review: (
    id: string,
    body: {
      approve: boolean;
      answers?: Record<string, unknown>;
      note?: string;
    },
  ) =>
    request<Application>(`/applications/${id}/review`, {
      method: "POST",
      body: JSON.stringify({ answers: {}, ...body }),
    }),

  otp: (id: string, code: string) =>
    request<Application>(`/applications/${id}/otp`, {
      method: "POST",
      body: JSON.stringify({ code }),
    }),

  /**
   * Tailor this posting with the local model and a cloud one, for a choice.
   *
   * `cloud` names the remote half for this comparison only. Omitted, it is
   * whatever real tailoring would use. Naming one moves no setting — the next
   * application routes exactly as it did before.
   */
  compareTailoring: (id: string, clouds?: string[]) =>
    request<Application>(`/applications/${id}/tailoring/compare`, {
      method: "POST",
      // `clouds` rather than `cloud`: each name is another upload of the
      // résumé to another third party, so the list is what the owner ticked
      // and never "everything with a key". Empty sends null, which the server
      // reads as "whatever real tailoring would use".
      body: JSON.stringify({
        clouds: clouds && clouds.length > 0 ? clouds : null,
      }),
    }),

  /**
   * Edit the résumé one application is about to send.
   *
   * Distinct from `editResume` in what it changes, not in how it saves. Both
   * version the document rather than mutating it; this one attaches the result
   * to a single application and leaves the profile's base alone, because on the
   * review screen the subject is one employer.
   */
  editApplicationResume: (
    id: string,
    body: {
      contact: {
        name?: string;
        email?: string;
        phone?: string;
        links?: string[];
      };
      sections: Record<string, string[]>;
      adopt?: boolean;
    },
  ) =>
    request<Application>(`/applications/${id}/resume/edit`, {
      method: "POST",
      body: JSON.stringify({ adopt: false, ...body }),
    }),

  /** Send this one. Restricted server-side to the versions that were compared. */
  selectTailoring: (id: string, resumeId: string) =>
    request<Application>(`/applications/${id}/tailoring/select`, {
      method: "POST",
      body: JSON.stringify({ resume_id: resumeId }),
    }),
};

/** Statuses that are waiting on the owner rather than on the machine. */
export const NEEDS_OWNER: ApplicationStatus[] = ["needs_review", "needs_otp"];

export function isTerminal(status: ApplicationStatus): boolean {
  return status === "submitted" || status === "failed";
}
