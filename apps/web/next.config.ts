import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

// The FastAPI app refuses non-loopback callers and has no CORS layer, on
// purpose (apps/api/middleware.py). Proxying through Next rather than calling
// :8000 from the browser keeps both of those properties: the browser only ever
// talks to its own origin, and the request that reaches FastAPI comes from this
// server over loopback.
const API_ORIGIN = process.env.JOBRUNNER_API ?? "http://127.0.0.1:8000";

// Next drops a proxied request that has sent nothing back for 30 seconds, and
// /chat sends nothing until the model has finished. A local model that Ollama
// had unloaded takes about that long to answer its first question, so the
// proxy cut it off and the assistant said the API could not be reached. The
// API has its own limit for every provider and its errors name the cause, so
// this only has to be longer than the longest of them (300 s, provider.py).
const PROXY_TIMEOUT_MS = 330_000;

export default function nextConfig(phase: string): NextConfig {
  return {
    // Keep `next dev` (`.next-dev`) separate from `next build` / `next start`
    // (`.next`) so running a build never overwrites an active dev server's
    // Webpack manifests and chunks (`__webpack_modules__[moduleId] is not a function`).
    distDir: phase === PHASE_DEVELOPMENT_SERVER ? ".next-dev" : ".next",
    experimental: { proxyTimeout: PROXY_TIMEOUT_MS },
    async rewrites() {
      return [{ source: "/api/:path*", destination: `${API_ORIGIN}/:path*` }];
    },
  };
}
