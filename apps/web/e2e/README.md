# Dashboard browser tests

Install once with `npm ci` and `npx playwright install chromium` from `apps/web`.
Run `npm --prefix apps/web run test:e2e` from the repository root. The default
uses Next development mode; `E2E_MODE=start npm --prefix apps/web run test:e2e`
builds and tests production mode. CI runs production mode.

The harness starts a synthetic Node API on `127.0.0.1:18321` and a temporary
copy of the real Next app on `127.0.0.1:13321`. Override `E2E_API_PORT` and
`E2E_WEB_PORT` for port conflicts. Existing servers are never reused. The copy
excludes local env files and build output, preserves the owner's `.next-dev`
and `.next`, and is removed when the test server stops. Production builds use
the mock API origin so browser rewrites and server requests share the fixture.

No database, LLM, secrets, browser sessions, or employer requests are needed.
After npm/browser installation the suite runs offline. Unexpected API calls
fail instead of falling through to a real service. Tests reset fixture state
and run serially; failures keep screenshots under ignored `test-results/`.
Tracing is disabled because its injected scripts trigger browser policy errors
inside the deliberately sandboxed résumé preview; console/page errors are
still asserted without exceptions.
