// Isolate Next manifests from the owner's running .next-dev and production build.
import { cpSync, mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

const root = process.cwd();
const dir = mkdtempSync(path.join(tmpdir(), "jobrunner-web-e2e-"));
const excluded = new Set(["node_modules", ".next", ".next-dev", "test-results", "playwright-report", ".env", ".env.local"]);
cpSync(root, dir, {
  recursive: true,
  filter: (source) => !path.relative(root, source).split(path.sep).some((part) =>
    excluded.has(part) || part.startsWith(".env.") || part.endsWith(".tsbuildinfo")),
});
symlinkSync(path.join(root, "node_modules"), path.join(dir, "node_modules"), "dir");
const next = path.join(root, "node_modules/next/dist/bin/next");
let child;
let stopping = false;
function cleanup() { rmSync(dir, { recursive: true, force: true }); }
function stop() {
  stopping = true;
  child?.kill("SIGTERM");
}
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
async function run(args) {
  return await new Promise((resolve) => {
    child = spawn(process.execPath, [next, ...args], { cwd: dir, env: process.env, stdio: "inherit" });
    child.on("error", () => resolve(1));
    child.on("exit", (code) => resolve(code ?? 1));
  });
}
try {
  const mode = process.argv[2] === "start" ? "start" : "dev";
  // Production rewrites are captured at build time: build against the mock too.
  if (mode === "start" && await run(["build"]) !== 0) process.exitCode = 1;
  else if (!stopping) process.exitCode = await run([mode, "-H", "127.0.0.1", "-p", process.env.JOBRUNNER_WEB_PORT ?? "13321"]);
} finally {
  cleanup();
}
