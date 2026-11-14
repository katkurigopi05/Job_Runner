# Contributing

Start with `README.md`, `CLAUDE.md`, and the `CLAUDE.md` in the directory you change. Use Python 3.12 and Node.js 22. Keep changes focused and preserve the local, single-user trust model.

## Checks

```bash
make install
make up
make migrate
make lint          # Ruff lint and format check
make typecheck     # Typed Python packages
make test          # Offline/unit suite; database tests need marked test DB
cd apps/web
npm ci
npm run lint
npm run typecheck
npm run test:dependencies
npm run build
```

Compose initializes `jobrunner_test` with the required `jobrunner:test` database comment on a fresh volume. Existing volumes may need that comment set explicitly. Set `TEST_DATABASE_URL` to this disposable database, never your live database. Tests refuse a live or unmarked database and serialize runs against one test database. `make gate-0` requires the database, checks migration drift, and runs the full suite; bare pytest can skip database tests if it is unavailable. Never call a real LLM from tests; use the existing stub/fake providers.

Benchmarks are experiments, not relevance claims; include seed, corpus size, parameters, and machine details with results.

## Adding an ATS adapter

Read `packages/ats/base.py` and `packages/ats/CLAUDE.md`. Add one adapter file under `packages/ats/`, keeping ATS-specific selectors in its `SELECTORS` table. Implement the shared inspect/fill/submit contract and register it through the existing registry. Use the adapter's field keys consistently in answers and fill reports.

Add a synthetic local form fixture and `tests/test_<adapter>.py`; exercise supported fields, required unanswered questions, redirects, unsupported hosts, and blocked/manual-completion paths. Never bypass CAPTCHA or anti-bot restrictions. Live tests must be explicit opt-ins against a posting you are authorized to use. Update supported-adapter documentation and the appropriate gate list if coverage changes.

## Pull request checklist

- Explain the concrete behavior change and include relevant validation results.
- Keep résumé claims source-grounded; never synthesize legal or employment-history answers.
- Preserve loopback defaults, the review gate, vault encryption, and state-machine transitions.
- Add focused regression coverage for behavior changes; apply schema migrations with rollback when needed.
- Use synthetic names, `example.com` emails, reserved example/invalid domains, and fictional phone numbers in fixtures. Exclude sessions, credentials, personal artifacts, and absolute machine paths.
- Run affected Python checks and the frontend checks when changing the dashboard.
- Use Conventional Commits; describe known limitations without overstating benchmark results.

By contributing, you agree that your contribution is provided under the repository's MIT license.
