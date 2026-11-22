"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { request } from "@/lib/api";

type Details = {
  round: string;
  date: string | null;
  time: string | null;
  timezone: string;
  format: string;
  duration_minutes: number | null;
  topics: string[];
};
type Source = {
  id: string;
  kind: string;
  reference: string;
  excerpt: string;
  summary: string;
  superseded?: boolean;
  url?: string;
  published?: string;
  retrieved?: string;
  caveat?: string;
  role?: string;
  level?: string;
  location?: string;
  round?: string;
};
type Coverage = {
  requirement: { id: string; wording: string };
  status: string;
  evidence_ids: string[];
  next_action: string;
};
type Block = { minutes: number; exercise: string };
type Mock = {
  stage: "question" | "followup" | "feedback";
  question: string;
  followup?: string;
  rubric: string[];
  mode?: string;
  feedback?: Record<string, string>;
};
type Revision = {
  id: string;
  round_id: string;
  status: string;
  queue_status?: string;
  attempts?: number;
  created_at: string;
  inputs: {
    details: Details;
    posting_ref: string;
    posting_version?: number;
    resume_ref: string;
    resume_id?: string;
    resume_version?: number;
    posting_description: string;
    posting_hash: string;
    invitation_id?: string;
    recruiter_excerpt?: string;
    prep_minutes: number;
  };
  progress: {
    evidence: Source[];
    plan: {
      topics: { topic: string; why: string; evidence_ids: string[] }[];
      next_hour: Block[];
      later: Block[];
      coverage: Coverage[];
      questions: string[];
      followups: string[];
      ask: string[];
      model_questions?: string[];
      model_followups?: string[];
      model_ask?: string[];
      caveats: string[];
    };
    steps: { action: string; count?: number; outcome?: string; elapsed_ms?: number }[];
    mock?: Mock;
  };
};
type Round = { id: string; task_id: string | null; notes: string | null; plans: Revision[] };
type Workspace = {
  company: string;
  role: string;
  fixture: boolean;
  submitted_resume: { id: string; version: number } | null;
  resumes: { id: string; version: number; created_at: string }[];
  rounds: Round[];
  invitations: {
    id: string; subject: string; at: string; inferred: boolean;
    round_id: string | null; suggested: Details;
  }[];
};

const EMPTY: Details = {
  round: "", date: null, time: null, timezone: "", format: "", duration_minutes: null, topics: [],
};
const field = "w-full rounded-lg border border-rule bg-paper px-3 py-2 text-sm text-ink";
const panel = "space-y-4 rounded-xl border border-rule bg-paper-high p-5";
const button = "rounded-lg border border-rule px-3 py-2 text-sm font-medium hover:bg-paper disabled:opacity-60";

function json(method: string, body: unknown) {
  return { method, body: JSON.stringify(body) };
}

export function Interview({ applicationId }: { applicationId: string }) {
  const root = `/applications/${applicationId}/interview`;
  const [data, setData] = useState<Workspace>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [targetRound, setTargetRound] = useState("");
  const [invitation, setInvitation] = useState("");
  const [details, setDetails] = useState(EMPTY);
  const [topics, setTopics] = useState("");
  const [note, setNote] = useState("");
  const [resume, setResume] = useState("");
  const [description, setDescription] = useState("");
  const [urls, setUrls] = useState("");
  const [minutes, setMinutes] = useState(60);
  const [research, setResearch] = useState(false);
  const [shareMail, setShareMail] = useState(false);
  const [requestId, setRequestId] = useState("");
  const [saved, setSaved] = useState("");

  const refresh = useCallback(async () => setData(await request<Workspace>(root)), [root]);
  useEffect(() => {
    refresh().catch((err: Error) => setError(err.message));
  }, [refresh]);
  const latest = data?.rounds.flatMap((round) => round.plans) ?? [];
  const running = latest.some((plan) =>
    ["queued", "running"].includes(plan.status) || ["pending", "running"].includes(plan.queue_status ?? ""));
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => { void refresh().catch(() => undefined); }, 2400);
    return () => window.clearInterval(timer);
  }, [refresh, running]);

  const revision = latest.find((item) => item.id === selected) ?? latest[0];
  const activeRound = data?.rounds.find((item) => item.id === revision?.round_id);
  const record = (value: Details) => {
    setDetails(value);
    setTopics(value.topics.join(", "));
  };
  function start(round?: Round) {
    setShowForm(true);
    setError("");
    setTargetRound(round?.id ?? "");
    setInvitation("");
    record(round?.plans[0]?.inputs.details ?? EMPTY);
    setNote("");
    const previous = round?.plans[0]?.inputs;
    setDescription("");
    setUrls("");
    setResume(previous?.resume_id ?? (previous ? "unknown" : ""));
    setResearch(false);
    setShareMail(false);
    setMinutes(round?.plans[0]?.inputs.prep_minutes ?? 60);
    setRequestId(crypto.randomUUID());
  }
  async function action(operation: () => Promise<void>) {
    setBusy(true);
    setError("");
    setSaved("");
    try {
      await operation();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The request failed.");
    } finally {
      setBusy(false);
    }
  }
  async function confirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await action(async () => {
      const result = await request<Revision>(root, json("POST", {
        request_id: requestId,
        round_id: targetRound || null,
        invitation_id: invitation || null,
        recruiter_note: note,
        details: { ...details, topics: topics.split(",").map((t) => t.trim()).filter(Boolean) },
        resume_id: resume && resume !== "unknown" ? resume : null,
        resume_unknown: resume === "unknown",
        description: description.trim() || null,
        prep_minutes: minutes,
        research_urls: urls.split(/\s+/).filter(Boolean),
        research,
        share_mail: research && shareMail,
      }));
      setSelected(result.id);
      setShowForm(false);
      await refresh();
    });
  }

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <header className="space-y-2">
        <Link className="text-sm underline underline-offset-4" href="/tracker">Tracker</Link>
        <h1 className="text-display">Interview prep</h1>
        {data && <p>{[data.company, data.role].filter(Boolean).join(" · ")}</p>}
        {data?.fixture && <p className="text-sm text-attn">Synthetic demo application and research fixtures</p>}
      </header>
      {error && <p role="alert" className="rounded-lg border border-rule p-3 text-attn">{error}</p>}
      {!data && !error && <p role="status">Loading interview prep…</p>}
      {data && (
        <>
          <div className="flex flex-wrap gap-3">
            <button className={button} onClick={() => start()}>Confirm a new round</button>
            {activeRound && <button className={button} onClick={() => start(activeRound)}>Reschedule or correct this round</button>}
          </div>
          {!revision && !showForm && <p>{data.invitations.length ? "Open the invitation to confirm the round and see your plan." : "Start with a recruiter phone note or enter the round details."}</p>}
          {showForm && (
            <form className={panel} onSubmit={confirm}>
              <h2 className="text-lg font-semibold">{targetRound ? "Confirm an updated schedule" : "Confirm interview details"}</h2>
              {data.invitations.length > 0 && (
                <div className="space-y-1">
                  <label htmlFor="prep-invitation">Invitation</label>
                  <select id="prep-invitation" className={field} value={invitation} onChange={(event) => {
                    const item = data.invitations.find((mail) => mail.id === event.target.value);
                    setInvitation(event.target.value);
                    if (item) {
                      record(item.suggested);
                      if (item.round_id) setTargetRound(item.round_id);
                    }
                  }}>
                    <option value="">Phone note or manual entry</option>
                    {data.invitations.map((mail) => (
                      <option key={mail.id} value={mail.id}>
                        {mail.subject || "Interview invite"} · {mail.at.slice(0, 10)}
                        {mail.inferred ? " · inferred application link" : ""}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {!invitation && (
                <div className="space-y-2">
                  <label className="block space-y-1">Recruiter phone note
                    <textarea className={field} value={note} maxLength={2500} onChange={(e) => setNote(e.target.value)} />
                  </label>
                  <button className={button} type="button" disabled={busy || !note.trim()}
                    onClick={() => void action(async () => record(await request<Details>(root + "/suggest", json("POST", { note }))))}>
                    Suggest details from note
                  </button>
                </div>
              )}
              <div className="grid gap-3 sm:grid-cols-3">
                {(["round", "date", "time", "timezone", "format", "duration_minutes"] as const).map((name) => (
                  <label key={name} className="space-y-1">
                    {{ round: "Round", date: "Date", time: "Time", timezone: "Time zone", format: "Format", duration_minutes: "Duration (minutes)" }[name]}
                    <input className={field} value={details[name] ?? ""}
                      type={name === "date" || name === "time" ? name : name === "duration_minutes" ? "number" : "text"}
                      onChange={(e) => setDetails({ ...details, [name]: name === "duration_minutes" ? Number(e.target.value) || null : e.target.value || (name === "date" || name === "time" ? null : "") })} />
                  </label>
                ))}
              </div>
              <label className="block space-y-1">Topics (comma separated)
                <input className={field} value={topics} onChange={(e) => setTopics(e.target.value)} />
              </label>
              {data.submitted_resume ? <p>Submitted résumé version {data.submitted_resume.version}</p> :
                <label className="block space-y-1">Submitted résumé
                  <select required className={field} value={resume} onChange={(e) => setResume(e.target.value)}>
                    <option value="">Choose a version</option>
                    <option value="unknown">Mark submitted version unknown</option>
                    {data.resumes.map((item) => <option value={item.id} key={item.id}>Version {item.version} · {item.created_at.slice(0, 10)} · {item.id.slice(0, 8)}</option>)}
                  </select>
                </label>}
              <label className="block space-y-1">Job description override (optional)
                <textarea className={field} value={description} maxLength={32000} onChange={(e) => setDescription(e.target.value)} />
              </label>
              <label className="block max-w-xs space-y-1">Time available to prepare (minutes)
                <input required className={field} min={5} max={6000} type="number" value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} />
              </label>
              {!data.fixture && <>
                <label className="block space-y-1">Public research URLs (one per line)
                  <textarea className={field} value={urls} onChange={(e) => setUrls(e.target.value)} />
                </label>
                <label className="flex items-center gap-2"><input type="checkbox" checked={research} onChange={(e) => setResearch(e.target.checked)} />Use Kaggle Qwen and share selected résumé and project excerpts for this plan</label>
                {research && <label className="flex items-center gap-2"><input type="checkbox" checked={shareMail} onChange={(e) => setShareMail(e.target.checked)} />Share recruiter invitation or phone-note excerpts with Kaggle for this plan</label>}
              </>}
              <button className={button} type="submit" disabled={busy}>Confirm and prepare</button>
              <button className={button + " ml-2"} type="button" onClick={() => setShowForm(false)}>Cancel</button>
            </form>
          )}
          {revision && activeRound && (
            <>
              <label className="block max-w-xl space-y-1">Round and revision
                <select className={field} value={revision.id} onChange={(e) => setSelected(e.target.value)}>
                  {data.rounds.map((round, index) => (
                    <optgroup key={round.id} label={round.plans[0]?.inputs.details.round || `Round ${data.rounds.length - index}`}>
                      {round.plans.map((plan, at) => <option key={plan.id} value={plan.id}>Revision {round.plans.length - at} · {plan.created_at.slice(0, 16).replace("T", " ")}</option>)}
                    </optgroup>
                  ))}
                </select>
              </label>
              <PlanView key={revision.id} root={root} plan={revision} round={activeRound} busy={busy} saved={saved}
                run={(operation, notice) => action(async () => { await operation(); await refresh(); setSaved(notice || ""); })} />
            </>
          )}
        </>
      )}
    </div>
  );
}

function Citation({ source }: { source: Source }) {
  return (
    <details className="my-2 rounded-lg border border-rule p-2 text-sm">
      <summary className="cursor-pointer">{source.kind} · {source.reference}</summary>
      <blockquote className="mt-2 whitespace-pre-wrap">{source.excerpt}</blockquote>
      {source.summary && <p className="mt-2">{source.summary}</p>}
      <p className="mt-2 text-xs text-ink-soft">
        {[source.role, source.level, source.location, source.round,
          source.published && `Published ${source.published}`,
          source.retrieved && `Retrieved ${source.retrieved.slice(0, 10)}`].filter(Boolean).join(" · ")}
      </p>
      {source.caveat && <p className="mt-2">{source.caveat}</p>}
      {source.url && /^https?:\/\//i.test(source.url) && <a className="mt-2 inline-block underline" href={source.url} target="_blank" rel="noreferrer">Open source</a>}
    </details>
  );
}

function PlanView({ root, plan, round, busy, saved, run }: {
  root: string; plan: Revision; round: Round; busy: boolean; saved: string;
  run: (operation: () => Promise<unknown>, notice?: string) => Promise<void>;
}) {
  const [notes, setNotes] = useState(round.notes ?? "");
  const [answer, setAnswer] = useState("");
  const [useKaggle, setUseKaggle] = useState(false);
  const [requirement, setRequirement] = useState("");
  const [evidence, setEvidence] = useState("");
  const [grade, setGrade] = useState(2);
  const base = root + "/plans/" + plan.id;
  const current = plan.progress.plan;
  const mock = plan.progress.mock;
  const candidate = plan.progress.evidence.filter((item) => !item.superseded && ["project", "resume", "coursework", "internship"].includes(item.kind));
  const citations = (ids: string[]) => ids.map((id) => {
    const source = plan.progress.evidence.find((entry) => entry.id === id);
    return source && <Citation key={id} source={source} />;
  });
  const details = plan.inputs.details;
  return (
    <>
      <section className={panel}>
        <h2 className="text-lg font-semibold">Confirmed round</h2>
        <p>{[details.round, details.date, details.time, details.timezone, details.format, details.duration_minutes && `${details.duration_minutes} minutes`].filter(Boolean).join(" · ") || "Schedule details are open."}</p>
        <p className="text-sm">{plan.inputs.resume_ref}{plan.inputs.resume_version && ` · version ${plan.inputs.resume_version}`}</p>
        <details className="text-sm">
          <summary>{plan.inputs.posting_ref}{plan.inputs.posting_version && ` · version ${plan.inputs.posting_version}`}</summary>
          <p className="my-2 text-xs">Snapshot hash {plan.inputs.posting_hash}</p>
          <p className="whitespace-pre-wrap">{plan.inputs.posting_description || "Job description unavailable."}</p>
        </details>
        {plan.inputs.recruiter_excerpt && <details className="text-sm"><summary>Original invitation or note</summary><p className="whitespace-pre-wrap">{plan.inputs.recruiter_excerpt}</p></details>}
      </section>
      <section className={panel}>
        <h2 className="text-lg font-semibold">What should I practice in the next hour?</h2>
        <ol className="list-decimal space-y-2 pl-5">
          {current.next_hour.map((block) => <li key={block.exercise}><b>{block.minutes} min</b> · {block.exercise}</li>)}
        </ol>
        {current.later.length > 0 && <div><h3 className="font-semibold">With more time</h3>{current.later.map((block) => <p key={block.exercise}>{block.minutes} min · {block.exercise}</p>)}</div>}
        <h3 className="font-semibold">Highest-priority topics</h3>
        <ol className="list-decimal space-y-3 pl-5">
          {current.topics.map((topic) => <li key={topic.topic}><b>{topic.topic}</b> · {topic.why}{citations(topic.evidence_ids)}</li>)}
        </ol>
      </section>
      <section className={panel}>
        <h2 className="text-lg font-semibold">Requirement coverage</h2>
        {!current.coverage.length && <p>No requirements could be extracted from the available description.</p>}
        {current.coverage.map((row) => (
          <article key={row.requirement.id} className="border-t border-rule pt-3">
            <h3 className="font-semibold">{row.requirement.wording}</h3>
            <p className="capitalize text-ink-soft">{row.status}</p>
            {citations(row.evidence_ids)}
            <p className="text-sm">{row.next_action}</p>
          </article>
        ))}
      </section>
      <section className={panel}>
        <h2 className="text-lg font-semibold">Research and sources</h2>
        <p role="status">Research status: {plan.status}{plan.queue_status && ` · worker ${plan.queue_status}`}{plan.attempts ? ` · attempt ${plan.attempts}` : ""}</p>
        {["interrupted", "failed", "cancelled"].includes(plan.status) && !["pending", "running"].includes(plan.queue_status ?? "") && <button className={button} disabled={busy} onClick={() => void run(() => request(base + "/retry", json("POST", {})))}>Resume research</button>}
        {current.caveats.map((caveat) => <p key={caveat} className="text-sm">{caveat}</p>)}
        {plan.progress.evidence.filter((item) => /guidance|report|public source|round notes/.test(item.kind)).map((item) => <Citation source={item} key={item.id} />)}
        <details className="text-sm"><summary>Research decisions and checkpoints</summary><pre className="mt-2 overflow-auto">{JSON.stringify(plan.progress.steps, null, 2)}</pre></details>
      </section>
      <section className={panel}>
        <h2 className="text-lg font-semibold">Practice and mock interview</h2>
        {[["Practice questions", current.questions], ["Likely follow-ups", current.followups], ["Ask the interviewer", current.ask]].map(([label, items]) => (
          <div key={String(label)}><h3 className="font-semibold">{label}</h3><ul className="list-disc pl-5">{(items as string[]).map((question, index) => <li key={index}>{question}</li>)}</ul></div>
        ))}
        {[current.model_questions, current.model_followups, current.model_ask].some((items) => items?.length) && (
          <details className="rounded-lg border border-rule p-3">
            <summary>Model-generated practice suggestions</summary>
            {[
              { label: "Questions", items: current.model_questions },
              { label: "Follow-ups", items: current.model_followups },
              { label: "Ask the interviewer", items: current.model_ask },
            ].filter(({ items }) => items?.length).map(({ label, items }) => (
              <div className="mt-2" key={label}>
                <h3 className="font-semibold">{label}</h3>
                <ul className="list-disc pl-5">{items?.map((item, index) => <li key={index}>{item}</li>)}</ul>
              </div>
            ))}
          </details>
        )}
        {!mock || mock.stage === "feedback" ? <button className={button} disabled={busy} onClick={() => void run(() => request(base + "/mock", json("POST", { action: "start" })))}>Start mock interview</button> : null}
        {mock && <div className="space-y-3 border-t border-rule pt-4">
          <p className="text-sm">Rubric: {mock.rubric.join(" · ")}</p>
          {mock.stage === "feedback" ? <div>
            <h3 className="font-semibold">Feedback · {mock.mode}</h3>
            {Object.entries(mock.feedback ?? {}).map(([criterion, value]) => <p key={criterion}><b>{criterion}</b> · {value}</p>)}
          </div> : <>
            <p className="font-semibold">{mock.stage === "question" ? mock.question : mock.followup}</p>
            <label className="block">Your answer<textarea className={field} maxLength={3000} value={answer} onChange={(e) => setAnswer(e.target.value)} /></label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={useKaggle} onChange={(e) => setUseKaggle(e.target.checked)} />Send this mock exchange to configured Kaggle for feedback</label>
            <button className={button} disabled={busy || !answer.trim()} onClick={() => void run(async () => {
              await request(base + "/mock", json("POST", { action: "answer", answer, turn: mock.stage, use_kaggle: useKaggle }));
              setAnswer("");
            })}>Submit answer</button>
          </>}
        </div>}
      </section>
      <section className={panel}>
        <h2 className="text-lg font-semibold">After the interview</h2>
        <label className="block">What was actually asked?<textarea className={field} maxLength={6000} value={notes} onChange={(e) => setNotes(e.target.value)} /></label>
        <button className={button} disabled={busy} onClick={() => void run(() => request(root + "/rounds/" + round.id + "/notes", json("PUT", { notes })), "Round notes saved.")}>Save round notes</button>
      </section>
      {candidate.length > 0 && current.coverage.length > 0 && <section className={panel}>
        <h2 className="text-lg font-semibold">Grade evidence relevance</h2>
        <label className="block">Requirement<select className={field} value={requirement} onChange={(e) => setRequirement(e.target.value)}><option value="">Select requirement</option>{current.coverage.map((row) => <option key={row.requirement.id} value={row.requirement.id}>{row.requirement.wording}</option>)}</select></label>
        <label className="block">Candidate excerpt<select className={field} value={evidence} onChange={(e) => setEvidence(e.target.value)}><option value="">Select evidence</option>{candidate.map((item) => <option key={item.id} value={item.id}>{item.reference} · {item.excerpt.slice(0, 160)}</option>)}</select></label>
        <label className="block">Relevance grade<select className={field} value={grade} onChange={(e) => setGrade(Number(e.target.value))}><option value="2">Directly relevant</option><option value="1">Somewhat relevant</option><option value="0">Irrelevant</option></select></label>
        <button className={button} disabled={busy || !requirement || !evidence} onClick={() => void run(() => request(base + "/judgment", json("PUT", { requirement, evidence, grade })), "Relevance grade saved.")}>Save judgment</button>
      </section>}
      {saved && <p role="status">{saved}</p>}
    </>
  );
}
