## Summary

<!-- Describe the behavior change and motivation. -->

## Verification

<!-- List exact commands run and observed pass/skip/fail counts. -->
- [ ] `make lint` (`ruff check . && ruff format --check .`)
- [ ] `make typecheck`
- [ ] Offline/DB tests (`make test` / `make gate-0`)
- [ ] Frontend checks (`npm --prefix apps/web run test:dependencies && npm --prefix apps/web run typecheck && npm --prefix apps/web run lint && npm --prefix apps/web run build`)

## Checklist

- [ ] Résumé generation remains strictly source-grounded; employment history and work authorization stay verbatim from profile.
- [ ] Loopback defaults (`127.0.0.1`), `AUTO_SUBMIT=false`, vault separation, and state-machine invariants are preserved.
- [ ] Any schema migration includes a tested downgrade/rollback path and passes `alembic check`.
- [ ] Tests use stub/mock LLM providers only and synthetic fixtures (`example.com`, `demo.invalid`, fictional phones).
- [ ] No `.env`, credentials, personal résumés, browser sessions, or local machine paths are included.
