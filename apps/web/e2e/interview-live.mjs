// Run against the isolated interview demo, using its real API and worker.
import { chromium, expect } from "@playwright/test";

const port = process.env.DEMO_WEB_PORT ?? "3001";
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_EXECUTABLE || undefined,
  headless: true,
});

try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`http://127.0.0.1:${port}/tracker`, { timeout: 45_000 });
  await page
    .getByRole("row")
    .filter({ hasText: "interview-demo.example.com" })
    .getByRole("link", { name: "Prepare for interview" })
    .click();
  await expect(page.getByText("Synthetic demo application and research fixtures")).toBeVisible();
  await page.getByRole("button", { name: "Confirm a new round", exact: true }).click();
  await page.getByLabel("Invitation", { exact: true }).selectOption({ index: 1 });
  await expect(page.getByLabel("Topics (comma separated)")).toHaveValue("Python, model evaluation");
  await page.getByLabel("Date", { exact: true }).fill("2026-12-04");
  await page.getByRole("button", { name: "Confirm and prepare" }).click();
  await expect(
    page.getByRole("heading", { name: "What should I practice in the next hour?" }),
  ).toBeVisible();
  await expect(page.getByText(/^Round 1 · 2026-12-04.*UTC/)).toBeVisible();
  await expect(page.getByText(/Research status: ready/)).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/senior system-design report is a weak match/i).first()).toBeVisible();
  const production = page
    .getByRole("article")
    .filter({ has: page.getByRole("heading", { name: /Kubernetes/ }) });
  await expect(production.getByText("missing", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Start mock interview" }).click();
  await page.getByLabel("Your answer").fill("I would add Python tests for normal and empty inputs.");
  await page.getByRole("button", { name: "Submit answer" }).click();
  await expect(page.getByText(/Pick one decision from your answer/)).toBeVisible();
  await page.getByLabel("Your answer").fill("Compare a reference solution against boundary cases.");
  await page.getByRole("button", { name: "Submit answer" }).click();
  await expect(page.getByRole("heading", { name: "Feedback · fixture" })).toBeVisible();
  await expect(page.getByText("Reasoning, trade-offs, and validation", { exact: true })).toBeVisible();
  await page.getByLabel("What was actually asked?").fill("They asked about tests for empty inputs.");
  await page.getByRole("button", { name: "Save round notes" }).click();
  await expect(page.getByText("Round notes saved.")).toBeVisible();
  if (errors.length) throw new Error(errors.join("\n"));
  console.log("Tracker, invitation, round confirmation, worker, mock and notes passed.");
} finally {
  await browser.close();
}
