/**
 * The Resume Studio's calls and shapes: `apps/api/routers/resume_studio.py`
 * and the evidence routes in `apps/api/routers/profiles.py`.
 *
 * Browser calls go through the same-origin `/api` proxy (`request`), which
 * keeps the API loopback-only.
 */

import { request, type Resume } from "@/lib/api";

export type RoleTrack =
  | "swe"
  | "fullstack"
  | "data_ml"
  | "ai_agentic"
  | "cloud_devops"
  | "fde_harness";

export const ROLE_TRACKS: { value: RoleTrack; label: string }[] = [
  { value: "swe", label: "SWE" },
  { value: "fullstack", label: "Full-Stack" },
  { value: "data_ml", label: "Data / ML" },
  { value: "ai_agentic", label: "AI / Agentic" },
  { value: "cloud_devops", label: "Cloud / DevOps" },
  { value: "fde_harness", label: "FDE / Harness" },
];

export interface Internship {
  id?: string;
  company: string;
  role: string;
  location: string | null;
  started: string;
  ended: string | null;
  stack: string[];
  bullets: string[];
  verified: boolean;
}

export interface Certification {
  id?: string;
  name: string;
  issuer: string;
  issued: string | null;
  expires: string | null;
  credential_id: string | null;
  skills: string[];
  verified: boolean;
}

export interface Evidence {
  candidate_id: string;
  internships: Internship[];
  certifications: Certification[];
}

export interface StudioRequest {
  candidate_id: string;
  profile_id: string | null;
  base_resume_id: string | null;
  job_description: string;
  role_track: RoleTrack;
  selected_project_ids: string[] | null;
  selected_certification_ids: string[] | null;
  include_internships: boolean;
  use_model: boolean;
}

export interface StudioTarget {
  term: string;
  evidence: string;
  kind: "bullet" | "practice" | "repository";
}

export interface StudioBullet {
  ref: string;
  original: string;
  tailored: string;
  status: "kept" | "rewritten" | "refused" | "provider failed" | "not asked";
  reason: string | null;
  targets: StudioTarget[];
}

export interface StudioLine {
  text: string;
  origin: "resume" | "internship" | "certification" | "rewrite";
  label: string;
  original: string | null;
}

export interface StudioAts {
  parse: number;
  keywords: number;
  overall: number;
  supported: string[];
  missing: string[];
  findings: string[];
}

export interface StudioProject {
  id: string;
  name: string;
  is_private: boolean;
  score: number;
  matched_terms: string[];
  chosen: boolean;
  held_back: string;
}

export interface StudioContact {
  name: string | null;
  email: string | null;
  phone: string | null;
  links: string[];
}

export interface StudioPreview {
  role_track: RoleTrack;
  role_label: string;
  base_resume_id: string;
  base_version: number;
  contact: StudioContact;
  sections: Record<string, string[]>;
  lines: Record<string, StudioLine[]>;
  html: string;
  bullets: StudioBullet[];
  terms: { term: string; status: string; sources: string[] }[];
  projects: StudioProject[];
  ats_before: StudioAts;
  ats_after: StudioAts;
  added: string[];
  skipped: string[];
  tailored_by: string | null;
  calls: number;
}

export interface StudioSave {
  candidate_id: string;
  profile_id: string | null;
  base_resume_id: string;
  role_track: RoleTrack;
  selected_project_ids: string[];
  selected_certification_ids: string[] | null;
  include_internships: boolean;
  contact: StudioContact;
  sections: Record<string, string[]>;
  rewrites: { original: string; tailored: string }[];
  owner_edits: string[];
  tailored_by: string | null;
  adopt: boolean;
}

export const studioApi = {
  evidence: (profileId: string) =>
    request<Evidence>(`/profiles/${profileId}/evidence`),
  saveEvidence: (
    profileId: string,
    body: { internships: Internship[]; certifications: Certification[] },
  ) =>
    request<Evidence>(`/profiles/${profileId}/evidence`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  synthesize: (body: StudioRequest) =>
    request<StudioPreview>("/resumes/synthesize", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  save: (body: StudioSave) =>
    request<{ resume: Resume; adopted: boolean }>("/resumes/synthesize/save", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

/** Comma-separated text to a clean list, and back. */
export function splitList(text: string): string[] {
  return text
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}
