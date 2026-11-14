"use client";

import { useMutation } from "@tanstack/react-query";
import {
  AlertTriangle,
  Award,
  Briefcase,
  CheckCircle2,
  Download,
  FileText,
  Lock,
  Pin,
  Plus,
  Save,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type {
  CertificationRecord,
  InternshipRecord,
  Profile,
  Project,
  Resume,
  ResumeSynthesizeOut,
  RoleTrack,
} from "@/lib/api";

interface Props {
  candidateId: string;
  profiles: Profile[];
  resumes: Resume[];
  projects: Project[];
  initialProfileId: string;
  initialBaseResumeId: string;
  initialInternships: InternshipRecord[];
  initialCertifications: CertificationRecord[];
}

const ROLE_TRACKS: Array<{ id: RoleTrack; label: string; sampleJd: string }> = [
  {
    id: "ai_agentic",
    label: "AI / Agentic AI",
    sampleJd:
      "Hiring an AI / Agentic Systems Engineer to build LLM tool-calling agents, RAG pipelines, Redis vector retrieval, evaluation harnesses, FastAPI services, and guardrails with Python and TypeScript.",
  },
  {
    id: "fde_harness",
    label: "FDE / Harness",
    sampleJd:
      "Forward Deployed & Harness Engineer working directly on customer agent harnesses, deterministic sandbox replay, CI/CD evaluation suites, FastAPI, Docker, Redis, and production debugging.",
  },
  {
    id: "data_ml",
    label: "Data / ML",
    sampleJd:
      "Data & ML Engineer building embedding ingestion, hybrid vector search (pgvector, Redis), offline evaluation pipelines, feature stores, and scalable Python services on AWS.",
  },
  {
    id: "fullstack",
    label: "Full-Stack",
    sampleJd:
      "Full-Stack Engineer building TypeScript, React, Next.js, TanStack Query interfaces backed by Python/FastAPI REST APIs, PostgreSQL, Redis, and automated tests.",
  },
  {
    id: "swe",
    label: "Software Engineer",
    sampleJd:
      "Software Engineer designing resilient distributed backend services, SQL schemas, REST APIs, automated testing suites, Docker deployments, and clean code.",
  },
  {
    id: "cloud_devops",
    label: "Cloud / DevOps",
    sampleJd:
      "Cloud & DevOps Engineer owning Docker, Kubernetes, Terraform, GitHub Actions CI/CD, Linux reliability, Prometheus observability, and AWS infrastructure.",
  },
];

function pct(score: number): string {
  return `${Math.round(score * 100)}%`;
}

export function StudioWorkspace({
  candidateId,
  profiles,
  resumes,
  projects,
  initialProfileId,
  initialBaseResumeId,
  initialInternships,
  initialCertifications,
}: Props) {
  const [profileId, setProfileId] = useState(initialProfileId);
  const [baseResumeId, setBaseResumeId] = useState(initialBaseResumeId);
  const [roleTrack, setRoleTrack] = useState<RoleTrack>("ai_agentic");
  const [jobDescription, setJobDescription] = useState(ROLE_TRACKS[0].sampleJd);
  const [useLlm, setUseLlm] = useState(false);

  const defaultSelectedIds = useMemo(() => {
    const eligible = projects.filter((p) => p.include !== false && !p.is_fork && !p.is_archived);
    return (eligible.length > 0 ? eligible : projects).map((p) => p.id);
  }, [projects]);

  const [selectedProjectIds, setSelectedProjectIds] = useState<string[]>(defaultSelectedIds);
  const [internships, setInternships] = useState<InternshipRecord[]>(initialInternships);
  const [certifications, setCertifications] = useState<CertificationRecord[]>(
    initialCertifications,
  );

  const [result, setResult] = useState<ResumeSynthesizeOut | null>(null);
  const [statusMessage, setStatusMessage] = useState("");
  const [errorMessage, setErrorMessage] = useState("");

  const saveEvidenceMutation = useMutation({
    mutationFn: async () => {
      if (!profileId) throw new Error("Create a profile first before saving Studio evidence.");
      const res = await fetch("/api/resumes/studio-evidence", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          profile_id: profileId,
          internships: internships.filter((i) => i.company.trim() && i.role.trim()),
          certifications: certifications.filter((c) => c.name.trim()),
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "Failed to save evidence");
      }
      return res.json();
    },
    onSuccess: () => {
      setErrorMessage("");
      setStatusMessage("Saved internships & certifications to profile.");
    },
    onError: (err: Error) => {
      setStatusMessage("");
      setErrorMessage(err.message);
    },
  });

  const synthesizeMutation = useMutation({
    mutationFn: async ({ saveVersion }: { saveVersion: boolean }) => {
      const res = await fetch("/api/resumes/synthesize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          candidate_id: candidateId,
          profile_id: profileId || null,
          base_resume_id: baseResumeId || null,
          role_track: roleTrack,
          job_description: jobDescription,
          project_ids: selectedProjectIds,
          project_limit: Math.max(1, Math.min(8, selectedProjectIds.length || 4)),
          internships: internships.filter((i) => i.company.trim() && i.role.trim()),
          certifications: certifications.filter((c) => c.name.trim()),
          use_llm: useLlm,
          save_version: saveVersion,
          adopt: saveVersion,
        }),
      });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(detail || `Synthesis failed (${res.status})`);
      }
      return (await res.json()) as ResumeSynthesizeOut;
    },
    onSuccess: (data, vars) => {
      setErrorMessage("");
      setResult(data);
      if (vars.saveVersion && data.saved_resume) {
        setBaseResumeId(data.saved_resume.id);
        setStatusMessage(
          `Saved résumé v${data.saved_resume.version} and set as active base version.`,
        );
      } else {
        setStatusMessage("Synthesized role-targeted preview.");
      }
    },
    onError: (err: Error) => {
      setStatusMessage("");
      setErrorMessage(err.message);
    },
  });

  const activeDownloadId = result?.saved_resume?.id || baseResumeId || resumes[0]?.id || "";

  const toggleProject = (id: string) => {
    setSelectedProjectIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  };

  const addInternship = () => {
    setInternships((prev) => [
      ...prev,
      { company: "", role: "", dates: "", location: "", stack: [], bullets: [] },
    ]);
  };

  const addCertification = () => {
    setCertifications((prev) => [
      ...prev,
      { name: "", issuer: "", date: "", credential_id: "", skills: [] },
    ]);
  };

  return (
    <div className="grid grid-cols-1 gap-8 xl:grid-cols-12">
      {/* Left pane: inputs & verified evidence */}
      <div className="space-y-6 xl:col-span-6">
        <section className="space-y-4 rounded-2xl border border-rule bg-paper-raised p-5 shadow-soft">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-mono text-xs uppercase tracking-[0.14em] text-ink-soft">
                1 · Target Role & Job Posting
              </h2>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {profiles.length > 0 ? (
                <select
                  aria-label="Target profile"
                  value={profileId}
                  onChange={(e) => setProfileId(e.target.value)}
                  className="max-w-full rounded-lg border border-rule bg-paper-warm px-2.5 py-1 font-mono text-xs text-ink focus:border-brand focus:outline-none"
                >
                  {profiles.map((p) => (
                    <option key={p.id} value={p.id}>
                      Profile: {p.label}
                    </option>
                  ))}
                </select>
              ) : null}
              {resumes.length > 0 ? (
                <select
                  aria-label="Base résumé"
                  value={baseResumeId}
                  onChange={(e) => setBaseResumeId(e.target.value)}
                  className="max-w-full rounded-lg border border-rule bg-paper-warm px-2.5 py-1 font-mono text-xs text-ink focus:border-brand focus:outline-none"
                >
                  <option value="">Synthesize without base résumé</option>
                  {[...resumes]
                    .sort((a, b) => b.version - a.version)
                    .map((r) => (
                      <option key={r.id} value={r.id}>
                        Base v{r.version}
                        {r.is_default ? " (default)" : ""}
                      </option>
                    ))}
                </select>
              ) : null}
            </div>
          </div>

          <div className="flex flex-wrap gap-2">
            {ROLE_TRACKS.map((track) => {
              const active = roleTrack === track.id;
              return (
                <button
                  key={track.id}
                  type="button"
                  onClick={() => {
                    setRoleTrack(track.id);
                    if (!jobDescription.trim() || ROLE_TRACKS.some((t) => t.sampleJd === jobDescription)) {
                      setJobDescription(track.sampleJd);
                    }
                  }}
                  className={`rounded-full border px-3 py-1 font-mono text-xs transition-colors ${
                    active
                      ? "border-brand bg-brand text-on-accent shadow-xs"
                      : "border-rule bg-paper-warm text-ink-soft hover:border-brand/60 hover:text-ink"
                  }`}
                >
                  {track.label}
                </button>
              );
            })}
          </div>

          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <label htmlFor="studio-jd" className="font-mono text-xs text-ink-soft">
                Paste Target Job Description
              </label>
              <button
                type="button"
                onClick={() => {
                  const found = ROLE_TRACKS.find((t) => t.id === roleTrack);
                  if (found) setJobDescription(found.sampleJd);
                }}
                className="font-mono text-[11px] text-brand underline-offset-4 hover:underline"
              >
                Load {ROLE_TRACKS.find((t) => t.id === roleTrack)?.label} sample
              </button>
            </div>
            <textarea
              id="studio-jd"
              rows={6}
              value={jobDescription}
              onChange={(e) => setJobDescription(e.target.value)}
              placeholder="Paste the job posting description, required skills, and responsibilities..."
              className="w-full rounded-xl border border-rule bg-paper-warm px-3.5 py-2.5 font-sans text-sm leading-relaxed text-ink focus:border-brand focus:outline-none"
            />
          </div>

          <label className="flex items-center gap-2 font-mono text-xs text-ink-soft">
            <input
              type="checkbox"
              checked={useLlm}
              onChange={(e) => setUseLlm(e.target.checked)}
              className="h-3.5 w-3.5 accent-[var(--color-brand)]"
            />
            <span>Run guarded LLM bullet tailoring (refuses unverified technologies/metrics)</span>
          </label>
        </section>

        {/* GitHub Repos (Private + Public) */}
        <section className="space-y-3 rounded-2xl border border-rule bg-paper-raised p-5 shadow-soft">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="font-mono text-xs uppercase tracking-[0.14em] text-ink-soft">
              2 · GitHub Repositories ({selectedProjectIds.length}/{projects.length} selected)
            </h2>
            <span className="font-mono text-[11px] text-ink-faint">
              Private & public repos synced under Résumés
            </span>
          </div>

          {projects.length === 0 ? (
            <p className="rounded-xl border border-dashed border-rule bg-paper-warm px-4 py-4 font-mono text-xs text-ink-soft">
              No repositories synced yet. Go to Résumés and sync your GitHub username (check Include
              private repositories + PAT for private work).
            </p>
          ) : (
            <div className="max-h-64 space-y-2 overflow-y-auto pr-1">
              {projects.map((project) => {
                const checked = selectedProjectIds.includes(project.id);
                return (
                  <label
                    key={project.id}
                    className={`flex cursor-pointer items-start gap-3 rounded-xl border px-3 py-2.5 text-sm transition-colors ${
                      checked
                        ? "border-brand/55 bg-paper-warm"
                        : "border-rule-soft bg-paper/60 opacity-75"
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleProject(project.id)}
                      className="mt-1 h-3.5 w-3.5 accent-[var(--color-brand)]"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs font-medium text-ink">
                          {project.name}
                        </span>
                        {project.is_private ? (
                          <Badge variant="attn" className="gap-1 px-2 py-0 text-[10px]">
                            <Lock className="h-2.5 w-2.5" aria-hidden="true" />
                            private
                          </Badge>
                        ) : null}
                        {project.pinned ? (
                          <Badge variant="default" className="gap-1 px-2 py-0 text-[10px]">
                            <Pin className="h-2.5 w-2.5" aria-hidden="true" />
                            pinned
                          </Badge>
                        ) : null}
                        {project.language ? (
                          <span className="font-mono text-[11px] text-ink-faint">
                            {project.language}
                          </span>
                        ) : null}
                      </div>
                      {project.description ? (
                        <p className="mt-1 text-xs text-ink-soft">{project.description}</p>
                      ) : null}
                    </div>
                  </label>
                );
              })}
            </div>
          )}
        </section>

        {/* Verified Internships */}
        <section className="space-y-3 rounded-2xl border border-rule bg-paper-raised p-5 shadow-soft">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="flex items-center gap-1.5 font-mono text-xs uppercase tracking-[0.14em] text-ink-soft">
              <Briefcase className="h-3.5 w-3.5" aria-hidden="true" />
              <span>3 · Verified Internships & Co-ops</span>
            </h2>
            <Button type="button" size="sm" variant="outline" onClick={addInternship}>
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Add Internship</span>
            </Button>
          </div>

          {internships.length === 0 ? (
            <p className="rounded-xl border border-dashed border-rule bg-paper-warm px-4 py-3 font-mono text-xs text-ink-faint">
              No studio internships added yet.
            </p>
          ) : (
            <div className="space-y-4">
              {internships.map((item, idx) => (
                <div
                  key={idx}
                  className="space-y-2.5 rounded-xl border border-rule-soft bg-paper-warm/70 p-3.5"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-ink-soft">
                      Internship #{idx + 1}
                    </span>
                    <button
                      type="button"
                      onClick={() =>
                        setInternships((prev) => prev.filter((_, i) => i !== idx))
                      }
                      aria-label={`Remove internship ${idx + 1}`}
                      className="text-ink-faint hover:text-stop"
                    >
                      <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                    </button>
                  </div>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <input
                      type="text"
                      value={item.role}
                      onChange={(e) =>
                        setInternships((prev) =>
                          prev.map((row, i) => (i === idx ? { ...row, role: e.target.value } : row)),
                        )
                      }
                      placeholder="Role (e.g. AI Engineering Intern)"
                      className="border border-rule bg-paper px-2.5 py-1.5 font-sans text-xs text-ink"
                    />
                    <input
                      type="text"
                      value={item.company}
                      onChange={(e) =>
                        setInternships((prev) =>
                          prev.map((row, i) =>
                            i === idx ? { ...row, company: e.target.value } : row,
                          ),
                        )
                      }
                      placeholder="Company"
                      className="border border-rule bg-paper px-2.5 py-1.5 font-sans text-xs text-ink"
                    />
                    <input
                      type="text"
                      value={item.dates}
                      onChange={(e) =>
                        setInternships((prev) =>
                          prev.map((row, i) =>
                            i === idx ? { ...row, dates: e.target.value } : row,
                          ),
                        )
                      }
                      placeholder="Dates (e.g. May 2025 – Aug 2025)"
                      className="border border-rule bg-paper px-2.5 py-1.5 font-mono text-xs text-ink"
                    />
                    <input
                      type="text"
                      value={item.location}
                      onChange={(e) =>
                        setInternships((prev) =>
                          prev.map((row, i) =>
                            i === idx ? { ...row, location: e.target.value } : row,
                          ),
                        )
                      }
                      placeholder="Location (optional)"
                      className="border border-rule bg-paper px-2.5 py-1.5 font-sans text-xs text-ink"
                    />
                  </div>
                  <input
                    type="text"
                    value={item.stack.join(", ")}
                    onChange={(e) =>
                      setInternships((prev) =>
                        prev.map((row, i) =>
                          i === idx
                            ? {
                                ...row,
                                stack: e.target.value
                                  .split(",")
                                  .map((s) => s.trim())
                                  .filter(Boolean),
                              }
                            : row,
                        ),
                      )
                    }
                    placeholder="Verified stack (comma-separated: Python, FastAPI, Redis)"
                    className="w-full border border-rule bg-paper px-2.5 py-1.5 font-mono text-xs text-ink"
                  />
                  <textarea
                    rows={3}
                    value={item.bullets.join("\n")}
                    onChange={(e) =>
                      setInternships((prev) =>
                        prev.map((row, i) =>
                          i === idx
                            ? {
                                ...row,
                                bullets: e.target.value
                                  .split("\n")
                                  .map((s) => s.trim())
                                  .filter(Boolean),
                              }
                            : row,
                        ),
                      )
                    }
                    placeholder="Impact bullets (one per line — only real metrics and tools you used)"
                    className="w-full border border-rule bg-paper px-2.5 py-1.5 font-sans text-xs text-ink"
                  />
                </div>
              ))}
            </div>
          )}
        </section>

        {/* Verified Certifications */}
        <section className="space-y-3 rounded-2xl border border-rule bg-paper-raised p-5 shadow-soft">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="flex items-center gap-1.5 font-mono text-xs uppercase tracking-[0.14em] text-ink-soft">
              <Award className="h-3.5 w-3.5" aria-hidden="true" />
              <span>4 · Certifications</span>
            </h2>
            <Button type="button" size="sm" variant="outline" onClick={addCertification}>
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Add Certification</span>
            </Button>
          </div>

          {certifications.length === 0 ? (
            <p className="rounded-xl border border-dashed border-rule bg-paper-warm px-4 py-3 font-mono text-xs text-ink-faint">
              No certifications added yet.
            </p>
          ) : (
            <div className="space-y-3">
              {certifications.map((cert, idx) => (
                <div
                  key={idx}
                  className="space-y-2 rounded-xl border border-rule-soft bg-paper-warm/70 p-3.5"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-ink-soft">
                      Certification #{idx + 1}
                    </span>
                    <button
                      type="button"
                      onClick={() =>
                        setCertifications((prev) => prev.filter((_, i) => i !== idx))
                      }
                      aria-label={`Remove certification ${idx + 1}`}
                      className="text-ink-faint hover:text-stop"
                    >
                      <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                    </button>
                  </div>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <input
                      type="text"
                      value={cert.name}
                      onChange={(e) =>
                        setCertifications((prev) =>
                          prev.map((row, i) => (i === idx ? { ...row, name: e.target.value } : row)),
                        )
                      }
                      placeholder="Certification Name (e.g. AWS Solutions Architect)"
                      className="border border-rule bg-paper px-2.5 py-1.5 font-sans text-xs text-ink"
                    />
                    <input
                      type="text"
                      value={cert.issuer}
                      onChange={(e) =>
                        setCertifications((prev) =>
                          prev.map((row, i) =>
                            i === idx ? { ...row, issuer: e.target.value } : row,
                          ),
                        )
                      }
                      placeholder="Issuer (e.g. Amazon Web Services)"
                      className="border border-rule bg-paper px-2.5 py-1.5 font-sans text-xs text-ink"
                    />
                    <input
                      type="text"
                      value={cert.date}
                      onChange={(e) =>
                        setCertifications((prev) =>
                          prev.map((row, i) => (i === idx ? { ...row, date: e.target.value } : row)),
                        )
                      }
                      placeholder="Year / Date"
                      className="border border-rule bg-paper px-2.5 py-1.5 font-mono text-xs text-ink"
                    />
                    <input
                      type="text"
                      value={cert.credential_id}
                      onChange={(e) =>
                        setCertifications((prev) =>
                          prev.map((row, i) =>
                            i === idx ? { ...row, credential_id: e.target.value } : row,
                          ),
                        )
                      }
                      placeholder="Credential ID (optional)"
                      className="border border-rule bg-paper px-2.5 py-1.5 font-mono text-xs text-ink"
                    />
                  </div>
                  <input
                    type="text"
                    value={cert.skills.join(", ")}
                    onChange={(e) =>
                      setCertifications((prev) =>
                        prev.map((row, i) =>
                          i === idx
                            ? {
                                ...row,
                                skills: e.target.value
                                  .split(",")
                                  .map((s) => s.trim())
                                  .filter(Boolean),
                              }
                            : row,
                        ),
                      )
                    }
                    placeholder="Verified skills covered (comma-separated)"
                    className="w-full border border-rule bg-paper px-2.5 py-1.5 font-mono text-xs text-ink"
                  />
                </div>
              ))}
            </div>
          )}
        </section>

        {/* Actions */}
        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            variant="default"
            disabled={synthesizeMutation.isPending}
            onClick={() => synthesizeMutation.mutate({ saveVersion: false })}
          >
            <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{synthesizeMutation.isPending ? "Synthesizing…" : "Synthesize Preview"}</span>
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={synthesizeMutation.isPending}
            onClick={() => synthesizeMutation.mutate({ saveVersion: true })}
          >
            <FileText className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Save Version & Set Active</span>
          </Button>
          {profileId ? (
            <Button
              type="button"
              variant="ghost"
              disabled={saveEvidenceMutation.isPending}
              onClick={() => saveEvidenceMutation.mutate()}
            >
              <Save className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Save Internships & Certs</span>
            </Button>
          ) : null}
        </div>

        {statusMessage ? (
          <p className="flex items-center gap-2 font-mono text-xs text-go" role="status">
            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{statusMessage}</span>
          </p>
        ) : null}
        {errorMessage ? (
          <p className="flex items-center gap-2 font-mono text-xs text-stop" role="alert">
            <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{errorMessage}</span>
          </p>
        ) : null}
      </div>

      {/* Right pane: ATS readout & formatted preview */}
      <div className="space-y-5 xl:col-span-6">
        <section className="space-y-4 rounded-2xl border border-rule bg-paper-raised p-5 shadow-soft">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-mono text-xs uppercase tracking-[0.14em] text-ink-soft">
                ATS Readiness & Rendered Document
              </h2>
              <p className="mt-1 text-xs text-ink-faint">
                Single-column parseable HTML, PDF, and DOCX — zero fabricated claims.
              </p>
            </div>
            {activeDownloadId ? (
              <div className="flex flex-wrap items-center gap-2">
                <a
                  href={`/api/resumes/${activeDownloadId}/docx`}
                  download
                  className="inline-flex h-8 items-center gap-1.5 rounded-full border border-brand bg-brand px-3.5 font-mono text-[11px] font-medium text-on-accent shadow-xs transition-opacity hover:opacity-90"
                >
                  <Download className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>ATS .docx</span>
                </a>
                <a
                  href={`/api/resumes/${activeDownloadId}/file`}
                  download
                  className="inline-flex h-8 items-center gap-1.5 rounded-full border border-rule bg-paper-warm px-3 font-mono text-[11px] font-medium text-ink transition-colors hover:border-brand"
                >
                  <Download className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>PDF</span>
                </a>
              </div>
            ) : null}
          </div>

          {result ? (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <div className="rounded-xl border border-rule-soft bg-paper-warm p-3">
                  <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-ink-faint">
                    ATS Parse
                  </div>
                  <div className="mt-1 font-display text-2xl text-ink">
                    {pct(result.ats_parse_score)}
                  </div>
                </div>
                <div className="rounded-xl border border-rule-soft bg-paper-warm p-3">
                  <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-ink-faint">
                    JD Match
                  </div>
                  <div className="mt-1 font-display text-2xl text-ink">
                    {pct(result.ats_keyword_score)}
                  </div>
                </div>
                <div className="rounded-xl border border-rule-soft bg-paper-warm p-3">
                  <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-ink-faint">
                    Projects Used
                  </div>
                  <div className="mt-1 font-display text-2xl text-ink">
                    {result.selected_projects.length}
                  </div>
                </div>
                <div className="rounded-xl border border-rule-soft bg-paper-warm p-3">
                  <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-ink-faint">
                    Guard Refusals
                  </div>
                  <div className="mt-1 font-display text-2xl text-ink">
                    {result.bullets_refused}
                  </div>
                </div>
              </div>

              {result.matched_terms.length > 0 ? (
                <div>
                  <div className="mb-1.5 font-mono text-[11px] uppercase tracking-[0.12em] text-go">
                    Matched JD Terms
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {result.matched_terms.map((term) => (
                      <Badge key={term} variant="go">
                        {term}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : null}

              {result.missing_terms.length > 0 ? (
                <div>
                  <div className="mb-1.5 font-mono text-[11px] uppercase tracking-[0.12em] text-ink-soft">
                    Missing From Candidate Corpus (never invented)
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {result.missing_terms.map((term) => (
                      <Badge key={term} variant="default">
                        {term}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : null}

              {result.ats_findings.length > 0 ? (
                <ul className="space-y-1 rounded-xl border border-attn/40 bg-attn-soft px-3.5 py-2.5 font-mono text-xs text-ink">
                  {result.ats_findings.map((finding, i) => (
                    <li key={i}>• {finding}</li>
                  ))}
                </ul>
              ) : null}

              <div className="overflow-hidden rounded-xl border border-rule bg-white shadow-soft">
                <iframe
                  title="Synthesized résumé preview"
                  srcDoc={result.html_preview}
                  sandbox=""
                  className="h-[740px] w-full bg-white"
                />
              </div>
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-rule bg-paper-warm px-6 py-24 text-center">
              <p className="font-mono text-xs uppercase tracking-[0.14em] text-ink-soft">
                Ready to synthesize
              </p>
              <p className="mx-auto mt-2 max-w-md text-sm text-ink-faint">
                Pick a target role track, select private/public repositories, add internships or
                certifications, and click <span className="font-mono text-ink">Synthesize Preview</span>{" "}
                to inspect the ATS score and formatted document before saving.
              </p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
