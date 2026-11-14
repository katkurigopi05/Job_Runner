// Deterministic loopback-only API for browser and Next Server Component requests.
// Unexpected calls fail loudly; this server never forwards to a real service.
import { createServer } from "node:http";

const now = "2026-01-15T12:00:00Z";
const candidateId = "00000000-0000-4000-8000-000000000001";
const profileId = "00000000-0000-4000-8000-000000000002";
const resumeId = "00000000-0000-4000-8000-000000000003";
const profile = {
  id: profileId, candidate_id: candidateId, label: "Software Engineering",
  base_resume_id: resumeId, phone: null, location: "Remote, US", work_auth: "Authorized",
  needs_sponsorship: false, citizenship_status: null, salary_expectation: null,
  min_match_score: 0.1, auto_submit: false, target_seniority: null,
  max_required_experience_years: null,
};
const resume = { id: resumeId, candidate_id: candidateId, version: 1, storage_ref: "synthetic", is_default: true, created_at: now };
const match = {
  id: "synthetic-match", profile_id: profileId, posting_id: "synthetic-posting", score: 0.3,
  decision: null, decided_at: null, title: "Software Engineer, Agent Tools",
  location: "Remote, US", url: "https://careers.example.com/jobs/agent-tools", ats_type: "greenhouse",
  first_seen_at: now, published_at: null, lag_hours: null, closed: false,
  board_unanswered: false, board_last_answered_at: null, title_similarity: 0.3, body_similarity: 0.3,
  matched_terms: ["Python"], missing_terms: [], legitimacy: null, rubric: null, excluded_by: [],
  eligibility: { summary: "Unknown — verify with employer", sponsorship: "unstated", citizenship: "unstated", evidence: [] },
  experience: null, compensation: null, requirements: null, sources: [], personalized_score: null,
  adjustments: [], keyword_hits: [],
};
const parsed = { contact: { name: "Alex Rivera", email: "alex@example.com" }, sections: { skills: ["Python"] }, raw_lines: ["Alex Rivera", "Skills", "Python"] };
let applications = [];
const emptyEvidence = () => ({ candidate_id: candidateId, internships: [], certifications: [] });
let evidence = emptyEvidence();
let rescorePending = false;
let crawlPending = false;
let calls = [];
const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  let raw = "";
  for await (const chunk of req) raw += chunk;
  const body = raw ? JSON.parse(raw) : null;
  const key = `${req.method} ${url.pathname}`;
  const json = (value, status = 200) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(value));
  };
  if (key === "POST /__reset") {
    applications = []; evidence = emptyEvidence();
    rescorePending = false; crawlPending = false; calls = [];
    return json({ ok: true });
  }
  if (key === "GET /__calls") return json(calls);
  calls.push({ method: req.method, path: url.pathname, query: url.search, body });
  if (key === "GET /health") return json({ status: "ok", api: "ok", database: "ok" });
  if (key === "GET /candidates") return json([{ id: candidateId, full_name: "Alex Rivera", email: "alex@example.com" }]);
  if (key === "GET /profiles") return json([profile]);
  if (key === `PATCH /profiles/${profileId}`) return json({ ...profile, ...body });
  if (key === "GET /resumes") return json([resume]);
  if (key === `GET /resumes/${resumeId}/parsed`) return json({ id: resumeId, version: 1, contact: parsed.contact, sections: { skills: 1 }, line_count: 3, parsed, layout: null });
  if (key === "GET /projects") return json([]);
  if (key === "GET /projects/entries") return json({ resume_id: resumeId, entries: [], projects: [] });
  if (key === "GET /applications") return json(applications);
  if (key === "POST /applications") {
    const app = { ...body, id: "synthetic-application", status: "queued", updated_at: now, created_at: now, outcome: null, questions_json: [], review_json: {}, failure_reason: null };
    applications.push(app); return json(app, 201);
  }
  if (key === "GET /matches") return json(url.searchParams.get("keywords") === "no-such-role" ? [] : [match]);
  if (key === "GET /matches/summary") return json({ total: 1, undecided: 1, interested: 0 });
  if (key === "GET /matches/calibration") return json({ decided: 0, enough_data: false });
  if (key === "GET /search-preferences/default") return json({ name: "default", filters: {} });
  if (key === "GET /analytics/digest") return json({ window_days: 7, postings_seen: 1, applications_created: 0, applications_submitted: 0, replies_received: 0, awaiting_review: 0, follow_ups_due: 0, quiet_week: true });
  if (["GET /inbox", "GET /inbox/unrouted", "GET /tasks"].includes(key)) return json([]);
  if (key === "GET /companies/status") return json({ companies_total: 0 });
  if (key === "GET /setup/status") return json({ overall: "ok", generated_at: now, items: [] });
  if (key === "GET /crawl/status") return json({ running: false, pending: crawlPending ? 1 : 0, stalled: crawlPending, last_finished_at: null, last_status: null, newest_posting_at: now, backlog: [] });
  if (key === "POST /crawl") { crawlPending = true; return json({ queued: true, already_waiting: 0, worker_alive: false }); }
  if (key === "POST /matches/rescore") { rescorePending = true; return json({ queued: true, already_waiting: 0, worker_alive: true }, 202); }
  if (key === "GET /matches/rescore") {
    const pending = rescorePending;
    rescorePending = false;
    return json({ pending: pending ? 1 : 0, running: pending, worker_alive: true, last_status: "done", last_finished_at: now });
  }
  if (key === "GET /events/applications") {
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    res.end(': deterministic offline stream\n\n'); return;
  }
  if (key === `GET /profiles/${profileId}/evidence`) return json(evidence);
  if (key === `PUT /profiles/${profileId}/evidence`) {
    const withIds = (list, prefix) => list.map((item, index) => ({ ...item, id: `${prefix}-${index}` }));
    evidence = {
      candidate_id: candidateId,
      internships: withIds(body.internships, "internship"),
      certifications: withIds(body.certifications, "certification"),
    };
    return json(evidence);
  }
  if (key === "POST /resumes/synthesize") {
    const added = evidence.internships.filter((item) => item.verified);
    const experience = added.flatMap((item) => [`${item.role}, ${item.company}  ${item.started}`, ...item.bullets]);
    const ats = { parse: 1, keywords: 0.5, overall: 0.8, supported: ["Python"], missing: ["Kubernetes"], findings: [] };
    return json({
      role_track: body.role_track, role_label: "Software Engineer", base_resume_id: resumeId, base_version: 1,
      contact: { ...parsed.contact, phone: null, links: [] },
      sections: { experience, skills: ["Python"] },
      lines: {
        experience: experience.map((text) => ({ text, origin: "internship", label: "Example Labs", original: null })),
        skills: [{ text: "Python", origin: "resume", label: "", original: null }],
      },
      html: "<h1>Alex Rivera</h1><p>Python</p>",
      bullets: [], terms: [{ term: "Python", status: "covered", sources: ["shared"] }, { term: "Kubernetes", status: "gap", sources: [] }],
      projects: [], ats_before: { ...ats, keywords: 0.5 }, ats_after: ats,
      added: added.map((item) => `${item.role}, ${item.company}`), skipped: [], tailored_by: null, calls: 0,
    });
  }
  if (key === "POST /resumes/synthesize/save") {
    return json({ resume: { ...resume, id: "00000000-0000-4000-8000-000000000004", version: 2, is_default: false }, adopted: body.adopt }, 201);
  }
  console.error(`Unexpected mock API request: ${key}`);
  json({ error: { code: "not_found", message: `No synthetic fixture for ${key}` } }, 404);
});
server.listen(Number(process.env.E2E_API_PORT ?? 18321), "127.0.0.1");
