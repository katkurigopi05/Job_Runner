import type { NextConfig } from "next";

// The FastAPI app refuses non-loopback callers and has no CORS layer, on
// purpose (apps/api/middleware.py). Proxying through Next rather than calling
// :8000 from the browser keeps both of those properties: the browser only ever
// talks to its own origin, and the request that reaches FastAPI comes from this
// server over loopback.
const API_ORIGIN = process.env.JOBRUNNER_API ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/:path*` }];
  },
};

export default nextConfig;
