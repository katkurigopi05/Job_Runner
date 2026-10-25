"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";
import { linkProject, syncProjects, type ProjectActionResult } from "@/app/resumes/actions";
import type { Project } from "@/lib/api";

const BUTTON =
  "rounded-[var(--radius)] border border-rule px-3 py-1.5 font-mono text-xs text-ink transition-colors hover:bg-paper-high disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-attn";

function Save({ label, pending: pendingLabel }: { label: string; pending: string }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" disabled={pending} className={BUTTON}>
      {pending ? pendingLabel : label}
    </button>
  );
}

function Status({ state }: { state: ProjectActionResult | null }) {
  if (!state) return null;
  return (
    <p role="status" className={`font-mono text-xs ${state.ok ? "text-go" : "text-stop"}`}>
      {state.message}
    </p>
  );
}

function RepositoryRow({
  project,
  entries,
  suggested,
}: {
  project: Project;
  entries: string[];
  suggested?: string;
}) {
  const [state, run] = useActionState<ProjectActionResult | null, FormData>(linkProject, null);
  const current = project.resume_entry ?? "";
  // A link to an entry this résumé no longer has is still shown, so it can be
  // seen and cleared rather than silently doing nothing.
  const options = current && !entries.includes(current) ? [current, ...entries] : entries;
  const selectId = `link-${project.id}`;

  return (
    <li className="grid gap-3 border-b border-rule py-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,18rem)]">
      <div className="min-w-0">
        <p className="flex flex-wrap items-baseline gap-2">
          <a
            href={project.url}
            className="font-display text-base underline-offset-4 hover:underline"
          >
            {project.name}
          </a>
          {project.is_private ? (
            <span className="font-mono text-xs text-ink-faint">private</span>
          ) : null}
        </p>
        {project.dependency_technologies.length > 0 ? (
          <p className="mt-1 text-sm text-ink-soft">
            {project.dependency_technologies.join(" · ")}
          </p>
        ) : (
          <p className="mt-1 font-mono text-xs text-ink-faint">
            no known tools in its dependency files
          </p>
        )}
      </div>

      <form action={run} className="flex flex-wrap items-center gap-2 self-start">
        <input type="hidden" name="project_id" value={project.id} />
        <label htmlFor={selectId} className="sr-only">
          Résumé project {project.name} backs
        </label>
        <select
          id={selectId}
          name="resume_entry"
          defaultValue={current || suggested || ""}
          className="min-w-0 flex-1 rounded-[var(--radius)] border border-rule bg-paper px-2 py-1.5 text-sm"
        >
          <option value="">not linked</option>
          {options.map((entry) => (
            <option key={entry} value={entry}>
              {entry}
            </option>
          ))}
        </select>
        <Save label="save" pending="saving…" />
        {!current && suggested ? (
          <p className="w-full font-mono text-xs text-ink-faint">
            suggested by name, not saved until you save it
          </p>
        ) : null}
        <div className="w-full">
          <Status state={state} />
        </div>
      </form>
    </li>
  );
}

export function ProjectLinks({
  candidateId,
  githubUser,
  projects,
  entries,
  suggestions,
}: {
  candidateId: string;
  githubUser: string;
  projects: Project[];
  entries: string[];
  suggestions: Record<string, string>;
}) {
  const [syncState, sync] = useActionState<ProjectActionResult | null, FormData>(
    syncProjects,
    null,
  );
  const own = projects.filter((project) => !project.is_fork);

  return (
    <section aria-labelledby="github-evidence" className="space-y-4">
      <header className="flex flex-wrap items-end gap-4 border-b border-rule pb-3">
        <div className="max-w-prose">
          <h2 id="github-evidence" className="font-display text-xl">
            GitHub evidence
          </h2>
          <p className="mt-1 text-sm text-ink-soft">
            Link a repository to the résumé project it backs. Tailoring may then name what that
            repository&apos;s own dependency files declare, on that project&apos;s bullets and no
            other&apos;s. Link only repositories whose code you wrote: a team project&apos;s
            dependencies include your teammates&apos; parts.
          </p>
        </div>
        <form action={sync} className="ml-auto flex flex-wrap items-center gap-2">
          <input type="hidden" name="candidate_id" value={candidateId} />
          <label htmlFor="github-user" className="sr-only">
            GitHub username
          </label>
          <input
            id="github-user"
            name="username"
            defaultValue={githubUser}
            placeholder="GitHub username"
            className="w-40 rounded-[var(--radius)] border border-rule bg-paper px-2 py-1.5 text-sm"
          />
          <Save label="sync from GitHub" pending="syncing…" />
        </form>
      </header>
      <Status state={syncState} />

      {own.length === 0 ? (
        <p className="border border-dashed border-rule px-6 py-10 text-center font-mono text-sm text-ink-faint">
          no repositories imported yet
        </p>
      ) : entries.length === 0 ? (
        <p className="font-mono text-sm text-ink-faint">
          this résumé has no Projects entries to link to
        </p>
      ) : (
        <ul>
          {own.map((project) => (
            <RepositoryRow
              key={project.id}
              project={project}
              entries={entries}
              suggested={suggestions[project.id]}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
