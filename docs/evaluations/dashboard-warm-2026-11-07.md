# Claude warm dashboard verification — 2026-11-07

The dashboard uses the Claude warm reference tokens and typography. Sidebar navigation is the default; the owner-requested Sidebar / Top nav control remembers the choice in browser storage. Changes are isolated on `feat/web-unslop`, based on main `aec0143988abd18c0db40968476745134cd31f9a`.

## Design passes

1. Exact warm dark/light surfaces, clay owner-action accent, toned success, a crimson stop colour distinct from clay, neutral in-flight and failed-bar states; no page glow.
2. Four-pixel spacing, 8px controls, 12px cards, 16px panels.
3. Grouped 13-link navigation, live Review count, service indicators, a Notes toggle in the sidebar footer, persistent layout selection, mobile menu.
4. Newsreader page titles, Instrument Sans interface and figures, JetBrains Mono for scores, ids and URLs, locale-formatted counts.
5. Status dots and sentence-case text; failure details remain available.
6. Inline Lucide paths with ISC attribution replace emoji controls; the drawn crawler remains.
7. Approval/profile copy preserved, clearer assistant empty state and Send action.
8. Desk date, waiting headline, primary queue action, four counts, matches, week/calibration panels, real stacked pipeline counts.
9. Off-white résumé sheet with dark text in both themes.
10. Explicit 150ms color/transform/opacity transitions, reduced-motion handling, approved beam and busy orb retained at their original placements.

No packages or lockfile changes. Fonts use Next's self-hosted build output. Assistant quick prompts were extracted, leaving `assistant.tsx` at 771 lines. No source tests were changed or weakened.

The beam library supports named presets rather than custom color props; its existing `sunset` preset remains. Explicit 12px rounding follows the new card shape. Stable wrappers, active-card selection, resolved theme and reduced-motion behavior remain.

The requested dataviz validator was unavailable in this session. Direct contrast calculation for the data blue against the new card surfaces gives 3.83:1 dark and 4.42:1 light (graphical-mark threshold: 3:1). The three semantic `--color-data` declarations remain pinned by tests.

## Checks

| Check | Result |
|---|---|
| Required 58-file Python selection, before edits | 1,040 passed |
| Required selection, final source | 1,040 passed |
| Gate 6 | 91 passed |
| Dependency compatibility tests | 7 passed |
| ESLint / TypeScript / production build | Passed |
| Package manifest and lockfile | Unchanged |
| Source line limit / whitespace | Passed |
| Browser interactions | Passed |
| Screenshot matrix | 72 views, rerun after the review fixes: all HTTP 200, no overflow or client errors |

Browser interaction checks cover both layouts and persistence, unsent assistant draft survival when switching layout, explicit theme precedence over the system preference, system-mode changes, Notes focus/Escape/focus return and draft retention, mobile menu ordering/Escape/navigation close, and 390px overflow.

## Browser evidence

The requested nine pages (`/`, `/review`, `/matches`, `/tracker`, `/chat`, `/applications`, `/resumes`, `/setup`, `/profile`) are checked at 1440px and 390px, dark and light, in both navigation layouts (72 views).

All local evidence is in `/private/tmp/jobrunner-unslop-artifacts/`; `index.html` is the screenshot gallery and `final-matrix.json` records status, overflow and client errors. Résumé, review and profile screenshots remain local because they contain personal information. Only Desk screenshots are committed below.

| Layout | Dark desktop | Light desktop | Dark mobile | Light mobile |
|---|---|---|---|---|
| Sidebar | [View](../screenshots/warm-dashboard/desk-sidebar-dark-1440.png) | [View](../screenshots/warm-dashboard/desk-sidebar-light-1440.png) | [View](../screenshots/warm-dashboard/desk-sidebar-dark-390.png) | [View](../screenshots/warm-dashboard/desk-sidebar-light-390.png) |
| Top nav | [View](../screenshots/warm-dashboard/desk-top-dark-1440.png) | [View](../screenshots/warm-dashboard/desk-top-light-1440.png) | [View](../screenshots/warm-dashboard/desk-top-dark-390.png) | [View](../screenshots/warm-dashboard/desk-top-light-390.png) |

## Review fixes, 2026-11-07

Five changes from review, with the required selection, build and 72-view matrix rerun afterwards.

1. **Failure no longer reads as clay.** The failed segment of the Desk pipeline bar is a neutral ramp step (`ink-soft` at 45%) instead of a tinted red. `--color-stop` is a distinct crimson in both modes (dark `#f2557a`, light `#b3173f`; stop surfaces `#46262f` and `#fae6ec`), so it cannot be mistaken for clay (`#d97757` / `#b5532f`) beside the needs-review dot.
2. **Figures in Instrument Sans.** Stat-card figures and This week values use Instrument Sans with `tabular-nums`. JetBrains Mono stays for scores, ids and URLs.
3. **Sentence case.** The Matches filter labels (Keywords, Role, Location, Remote, At least, Seen within, Order, Pay reaching, Skills I lack, Must name, My education, Sponsorship), the résumé section headings on Review such as "Skills · 8", and "Recruiter mail: included. It never leaves this machine." on Assistant.
4. **No empty top bar.** The 48px bar that held only the notes icon is gone. Notes is a labelled toggle in the sidebar footer, with the same focus-to-textarea, Escape-to-close and focus-return behaviour. The panel opens beside the sidebar, below the footer in Top nav, and inline in the mobile menu.
5. **Retry time matches the Desk date.** The Matches retry time reads like "Sunday 8 November, 22:08", pinned to `America/Los_Angeles`.

## Matches hydration fix

Early production screenshot runs exposed intermittent React #418 HTML mismatches on Matches. A test-browser-only instrumented React bundle captured a requirements-section host claim whose hydration cursor was already at its first pay child; its serialized children also contained a Flight lazy element. Development reloads did not reproduce it. This localizes the failure, but does not establish an upstream React root cause.

`RequirementsPanel` is now a client boundary with serializable props. Pay, skills and education render as one synchronous, deterministic subtree in both the server preview and browser hydration, avoiding partially resolved Flight children inside this section. Extraction, filtering and API reads remain on the server. No API or approval behavior changes. First-load Matches JavaScript is 111 kB in the final build.

The focused follow-up passed with no client errors or overflow. It loads Matches three times in each layout/theme/width combination (24 total), checks all 50 requirement panels, captures its eight screenshots again, and records page errors and overflow in `matches-final.json`. The other 64 matrix screenshots are unchanged by this isolated component boundary.

## Contracts retained

The required selection was run before and after editing. Styling-only pins did not require changes. The following checks protect behavior rather than old visual styling:

| Family | Preserved contract |
|---|---|
| Effects/dependencies | Exact package set; one Review beam and one busy assistant orb; stable active-card wrapper; explicit theme and reduced motion |
| Job links | Existing `POSTING_LINK` tab/opener policy, including assistant citations and expanded source lists |
| Assistant UI | Streamed reply, citations, sources, edit/reask, copy, safe reply rendering, chart display, retrieval and count wording |
| Vocabulary/tracker | API enum values, seven outcomes and fallback column; no outcome silently disappears |
| Crawler | Checking, working, stalled, idle and unavailable states; stale responses never imply current work |
| Matches/filters | Rank-based verdicts, matched terms, board-health wording, role/seniority/education/sponsorship/experience options and URL semantics |
| Approval/security | Reconciliation wording, verbatim profile answers, loopback binding, origin/rebinding guards, provider proxy timeout |
| Chart styling pins | Semantic `bg-data`, exactly three six-digit `--color-data` declarations, and `opacity-40` with “too few” wording |

### Full required test selection

“Source” means the test reads frontend source; the remaining tests protect the associated API/retrieval behavior. Descriptions below are from each test module's stated purpose.

| Test file | Reads source | Purpose |
|---|---|---|
| `tests/test_assistant_copy_edit.py` | Yes | A turn in the assistant can be copied, and a question edited and asked again. |
| `tests/test_assistant_dock_sources.py` | Yes | The assistant dock shows every posting the search found, not only the cited ones. |
| `tests/test_assistant_dock_streams.py` | Yes | The dock shows the answer as it is written. |
| `tests/test_assistant_reply_display.py` | Yes | How the dock shows a reply: lists and tables laid out, charts drawn, never as HTML. |
| `tests/test_chat_answer_modes.py` | No | Mode-specific prompts and guards for the captured prose-format failure. |
| `tests/test_chat_answer_support.py` | No | Answer support is a human judgment, separate from valid citation labels. |
| `tests/test_chat_api.py` | No | The assistant — CLAUDE.md §2.2 and §2.8 applied to a chat surface. |
| `tests/test_chat_company_applications.py` | No | Synthetic applications must not inherit the global status counts. |
| `tests/test_chat_connecting_words.py` | No | A word that joins a role to its subject is not something to search for. |
| `tests/test_chat_context_budget.py` | No | Opt-in packing preserves facts and removes only entire posting excerpts. |
| `tests/test_chat_crawl_command.py` | No | Saying "run crawler" or "find new jobs" to the assistant starts a crawl. |
| `tests/test_chat_cross_encoder.py` | No | A cross-encoder as the assistant's second stage, and the one rule it loosens. |
| `tests/test_chat_facts.py` | No | Synthetic regressions for factual answers and their count scopes. |
| `tests/test_chat_figures.py` | No | Rates and charts in the assistant come from code, never from the model. |
| `tests/test_chat_grounding.py` | No | What the assistant's reply can and cannot say for itself. |
| `tests/test_chat_intent_routing.py` | No | Two ways of asking one thing take one path. |
| `tests/test_chat_job_type_words.py` | No | 'Software jobs' asks for a kind of job, so it is read from titles. |
| `tests/test_chat_judgment_api.py` | No | `/judgments`: serving the pool and recording the owner's grades (Phase 5). |
| `tests/test_chat_judgment_pool.py` | No | The pool of postings the owner grades for the assistant's search (Phase 5). |
| `tests/test_chat_judgment_scores.py` | Yes | Scoring the assistant's search against the owner's grades (Phase 5). |
| `tests/test_chat_lists_every_match.py` | No | A reply lists every posting it found, not the first hundred. |
| `tests/test_chat_memory.py` | Yes | What the assistant remembers of a conversation. |
| `tests/test_chat_open_posting_count.py` | No | 'How many jobs are available?' is answered with the counts, in code. |
| `tests/test_chat_posting_share.py` | No | Counts and shares are facts computed over the same search area. |
| `tests/test_chat_protected_questions.py` | No | §2.2, asked in a sentence rather than named as a field. |
| `tests/test_chat_requirement_versions.py` | No | Selected chat evidence uses current readings without rewriting stored postings. |
| `tests/test_chat_rerank.py` | No | A second model re-orders what the assistant's search found. |
| `tests/test_chat_retrieval.py` | No | Retrieval for the assistant — the R in front of the chat model. |
| `tests/test_chat_role_questions.py` | Yes | A question naming a kind of job finds the jobs of that kind, and says how many. |
| `tests/test_chat_selection.py` | No | A model selects existing evidence; it cannot supply replacement facts. |
| `tests/test_chat_selection_prompt_split.py` | No | The answer prompt carries no selection protocol, and a bare selection is never shown. |
| `tests/test_chat_selection_routes.py` | No | Posting answers validate the entire model output before either route displays facts. |
| `tests/test_chat_semantic_candidates.py` | No | Opt-in semantic candidates can recover paraphrases outside the keyword pool. |
| `tests/test_chat_seniority.py` | No | A level named in a question is read from titles, not searched for as a word. |
| `tests/test_chat_skill_gaps.py` | No | The assistant, synced with the owner's résumés: "what am I lagging on?". |
| `tests/test_chat_sponsorship.py` | No | A question about sponsorship is answered from what postings say about it. |
| `tests/test_chat_stream.py` | No | `POST /chat/stream`: the assistant's answer, shown as it is written. |
| `tests/test_chat_structured_constraints.py` | No | Recorded requirement labels and exact title matches do not need model judgment. |
| `tests/test_chat_tiers.py` | Yes | What matches all of a question first, then the rest after it, and nothing dropped. |
| `tests/test_chat_title_words.py` | Yes | A word too common to search descriptions for is still telling in a title. |
| `tests/test_ci_frontend.py` | Yes | The dashboard is checked in CI, with the commands a developer runs. |
| `tests/test_crawler_spider_ui.py` | Yes | Controlled fixtures for the browser-facing crawl indicator state. |
| `tests/test_dashboard_binds_loopback.py` | Yes | The dashboard must not put the unauthenticated API on the network. |
| `tests/test_dashboard_effects.py` | Yes | The dashboard's two effect libraries, and the one meaning each is allowed. |
| `tests/test_dashboard_job_links.py` | Yes | Where the dashboard's job links open, and the tab name that replaced it. |
| `tests/test_dashboard_proxy_outlasts_providers.py` | Yes | The dashboard's proxy must not give up on the assistant before a model does. |
| `tests/test_dashboard_security_dependencies.py` | Yes | No installed minimatch engine may regain the globparts amplification bug. |
| `tests/test_dashboard_vocabulary.py` | Yes | A vocabulary the dashboard names is the one the API sends. |
| `tests/test_experience_requirements.py` | Yes | How many years a posting demands, and how firmly — read from real postings. |
| `tests/test_feed_reads_as_encouraging.py` | Yes | The feed has to show what you have, and rank rather than grade. |
| `tests/test_matches_board_health.py` | Yes | A posting on a board that has stopped answering says so on its card. |
| `tests/test_rebinding_guard.py` | Yes | A page on another site must not reach the API through the owner's browser. |
| `tests/test_retrieval_totals.py` | Yes | Exact totals only for one keyword or an employer with no extra subject. |
| `tests/test_role_filter.py` | Yes | The feed can be asked for one kind of role, by a dropdown rather than a guess. |
| `tests/test_security_origins.py` | Yes | Exercise both real origin guards without reaching a database or employer. |
| `tests/test_sponsorship_filter.py` | Yes | The feed can be asked about work authorization, and an unknown never passes. |
| `tests/test_submission_reconciliation.py` | Yes | Settling a submission nobody can vouch for, after asking the employer. |
| `tests/test_tracker_columns.py` | Yes | The tracker's columns must be `Outcome` values, because the API sends those. |
