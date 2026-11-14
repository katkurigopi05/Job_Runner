import { test as base, expect } from "@playwright/test";

// Automatic fixture observes the whole test, including late hydration/mutation errors.
const test = base.extend<{ runtimeErrors: string[] }>({
  runtimeErrors: [async ({ page }, use) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    await use(errors);
    expect(errors, "browser runtime/console errors").toEqual([]);
  }, { auto: true }],
});
const apiOrigin = `http://127.0.0.1:${process.env.E2E_API_PORT ?? "18321"}`;

test.beforeEach(async ({ request }) => {
  expect((await request.post(`${apiOrigin}/__reset`)).ok()).toBeTruthy();
});

for (const [route, heading] of [
  ["/", /waiting|desk|next|start|ready/i],
  ["/matches", "Matches"],
  ["/review", "Review queue"],
  ["/resumes", "Résumés"],
  ["/resumes/studio", "Resume Studio"],
  ["/tracker", "Tracker"],
  ["/setup", "Everything this installation needs is in place."],
  ["/applications", "Pipeline"],
] as const) {
  test(`renders ${route} cleanly`, async ({ page }) => {
    const response = await page.goto(route);
    expect(response?.status()).toBe(200);
    await expect(page.locator("main h1")).toContainText(heading);
    await expect(page.getByText("Cannot reach the jobrunner API", { exact: false })).toHaveCount(0);
    // Wait for a browser API response too, so this exercises hydration.
    await expect(page.getByText("API on this machine only", { exact: true })).toBeVisible();
  });
}

test("refresh reloads the server route", async ({ page, request }) => {
  await page.goto("/matches");
  const before = (await (await request.get(`${apiOrigin}/__calls`)).json()).filter(
    (call: { path: string }) => call.path === "/matches",
  ).length;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(async () => (await (await request.get(`${apiOrigin}/__calls`)).json()).filter(
    (call: { path: string }) => call.path === "/matches",
  ).length).toBeGreaterThan(before);
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeEnabled();
});

test("crawl and rescore show worker feedback and completion", async ({ page }) => {
  await page.goto("/matches");
  await page.getByRole("button", { name: "Run Crawl", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "No live worker" })).toBeVisible();
  await page.getByRole("button", { name: "Rescore Matches", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Worker is alive" })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "Rescore complete" })).toBeVisible();
});

test("queue application uses server action and reflects queued status", async ({ page, request }) => {
  await page.goto("/matches");
  await page.getByRole("button", { name: "Queue & tailor", exact: true }).click();
  await expect(page.getByRole("link", { name: "Queued", exact: true })).toBeVisible();
  const calls = await (await request.get(`${apiOrigin}/__calls`)).json();
  const queued = calls.find((call: { method: string; path: string }) => call.method === "POST" && call.path === "/applications");
  expect(queued.body.profile_id).toBe("00000000-0000-4000-8000-000000000002");
  expect(queued.body.url).toBe("https://careers.example.com/jobs/agent-tools");
});

test("matches filters reach server requests and validation stays beside controls", async ({ page, request }) => {
  await page.goto("/matches");
  await page.getByRole("combobox", { name: "Remote", exact: true }).selectOption("true");
  await expect(page).toHaveURL(/remote=true/);
  await page.getByRole("textbox", { name: "Keywords", exact: true }).fill("no-such-role");
  await page.getByRole("textbox", { name: "Keywords", exact: true }).press("Enter");
  await expect(page.getByText("nothing scored yet", { exact: true })).toBeVisible();
  const calls = await (await request.get(`${apiOrigin}/__calls`)).json();
  expect(calls.some((call: { path: string; query: string }) => call.path === "/matches" && call.query.includes("remote=true") && call.query.includes("keywords=no-such-role"))).toBeTruthy();
  await page.getByRole("spinbutton", { name: "minimum pay" }).fill("-1");
  await page.getByRole("spinbutton", { name: "minimum pay" }).press("Tab");
  await expect(page.getByRole("alert").filter({ hasText: "Minimum pay" })).toHaveText("Minimum pay must be zero or a positive number.");
  expect(page.url()).not.toContain("min_salary");
});

test("Studio saves synthetic evidence and synthesizes a deterministic preview", async ({ page, request }) => {
  await page.goto("/resumes/studio");
  await page.getByRole("button", { name: /Add internship/i }).click();
  await page.getByPlaceholder("Company", { exact: true }).fill("Example Labs");
  await page.getByPlaceholder("Role (e.g. AI Engineering Intern)").fill("Engineering Intern");
  await page.getByRole("button", { name: "Save Internships & Certs", exact: true }).click();
  await expect(page.getByText("Saved internships & certifications to profile.", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Synthesize Preview", exact: true }).click();
  await expect(page.getByText("Synthesized role-targeted preview.", { exact: true })).toBeVisible();
  const previewFrame = page.locator('iframe[title="Synthesized résumé preview"]');
  await expect(previewFrame).toBeVisible();
  await expect(previewFrame).toHaveAttribute("sandbox", "");
  await expect(previewFrame).toHaveAttribute("srcdoc", /Alex Rivera/);
  const calls = await (await request.get(`${apiOrigin}/__calls`)).json();
  const evidence = calls.find((call: { method: string }) => call.method === "PUT");
  expect(evidence.body.internships[0].company).toBe("Example Labs");
  const synthesis = calls.find((call: { path: string }) => call.path === "/resumes/synthesize");
  expect(synthesis.body.save_version).toBe(false);
  expect(synthesis.body.use_llm).toBe(false);
});

test("profile accepts fractional scores and rejects out-of-range input", async ({ page, request }) => {
  await page.goto("/profile");
  const score = page.getByRole("spinbutton", { name: "Minimum match score", exact: true });
  await score.fill("1.5");
  await page.getByRole("button", { name: "save", exact: true }).click();
  expect(await score.evaluate((input: HTMLInputElement) => input.validity.rangeOverflow)).toBeTruthy();
  await score.fill("0.25");
  await page.getByRole("button", { name: "save", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: /saved/i })).toBeVisible();
  const calls = await (await request.get(`${apiOrigin}/__calls`)).json();
  const updates = calls.filter((call: { method: string }) => call.method === "PATCH");
  expect(updates).toHaveLength(1);
  expect(updates[0].body.min_match_score).toBe(0.25);
});
