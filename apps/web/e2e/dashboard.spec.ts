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
  await page.getByRole("button", { name: "Pause Crawl", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Crawler paused" })).toBeVisible();
  await page.getByRole("button", { name: "Resume Crawl", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Crawler resumed" })).toBeVisible();
  await page.getByRole("button", { name: "Stop Crawl", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Crawler stopped (2 pending tasks cancelled)." })).toBeVisible();
  await page.getByRole("button", { name: /Workers \(/ }).click();
  const region = page.getByRole("region", { name: "Worker controls" });
  await expect(region).toBeVisible();
  await region.getByRole("combobox", { name: "Max workers" }).selectOption("4");
  await expect(page.getByRole("status").filter({ hasText: "max 4 workers" })).toBeVisible();
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

test("Studio saves confirmed evidence, synthesizes a traced draft, and saves a version", async ({ page, request }) => {
  await page.goto("/resumes/studio");
  await page.getByRole("button", { name: /Add internship/i }).click();
  await page.getByRole("button", { name: "Save internships and certifications", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Enter a company." })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "Enter a role." })).toBeVisible();
  expect((await (await request.get(`${apiOrigin}/__calls`)).json()).filter((call: { method: string }) => call.method === "PUT")).toHaveLength(0);
  await page.getByLabel("Company", { exact: true }).fill("Example Labs");
  await page.getByLabel("Role", { exact: true }).fill("Engineering Intern");
  await page.getByLabel("Started", { exact: true }).fill("Jun 2025");
  await page.getByLabel(/What you did/).fill("Built a test harness that cut flaky runs by 30%.");
  await page.getByLabel(/I confirm this is accurate/).check();
  await page.getByRole("button", { name: /Add certification/i }).click();
  await page.getByRole("button", { name: "Save internships and certifications", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Enter a certification name." })).toBeVisible();
  await page.getByLabel("Certification", { exact: true }).fill("Cloud Fundamentals");
  await page.getByRole("button", { name: "Save internships and certifications", exact: true }).click();
  await expect(page.getByText("Saved.", { exact: true })).toBeVisible();

  await page.getByLabel("Paste the job description").fill("Requirements: Python and Kubernetes.");
  await page.getByRole("button", { name: /Synthesize draft/ }).click();
  await expect(page.getByRole("heading", { name: "The draft, line by line" })).toBeVisible();
  await expect(page.getByText("Engineering Intern, Example Labs  Jun 2025", { exact: true })).toBeVisible();
  await expect(page.getByText("No model was asked.", { exact: true })).toBeVisible();
  const previewFrame = page.locator('iframe[title="Print preview"]');
  await expect(previewFrame).toHaveAttribute("sandbox", "");
  await expect(previewFrame).toHaveAttribute("srcdoc", /Alex Rivera/);

  await page.getByRole("button", { name: "Save as a new version", exact: true }).click();
  await expect(page.getByText("Saved as v2.", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "DOCX" })).toHaveAttribute("href", /\/api\/resumes\/.+\/docx$/);

  const calls = await (await request.get(`${apiOrigin}/__calls`)).json();
  const evidence = calls.find((call: { method: string }) => call.method === "PUT");
  expect(evidence.body.internships[0]).toMatchObject({ company: "Example Labs", verified: true });
  const synthesis = calls.find((call: { path: string }) => call.path === "/resumes/synthesize");
  expect(synthesis.body.use_model).toBe(false);
  const saved = calls.find((call: { path: string }) => call.path === "/resumes/synthesize/save");
  expect(saved.body.rewrites).toEqual([]);
  expect(saved.body.owner_edits).toEqual([]);
});

test("profile accepts fractional scores and rejects out-of-range input", async ({ page, request }) => {
  await page.goto("/profile");
  const score = page.getByRole("spinbutton", { name: "Minimum match score", exact: true });
  await score.fill("1.5");
  await page.getByRole("button", { name: "save", exact: true }).click();
  expect(await score.evaluate((input: HTMLInputElement) => input.validity.rangeOverflow)).toBeTruthy();
  // Exercise the schema too, even if native browser validation is bypassed.
  await score.evaluate((input: HTMLInputElement) => { input.form!.noValidate = true; });
  await page.getByRole("button", { name: "save", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Minimum match score" })).toHaveText("Minimum match score must be a number from 0 to 1.");
  expect((await (await request.get(`${apiOrigin}/__calls`)).json()).filter((call: { method: string }) => call.method === "PATCH")).toHaveLength(0);
  await score.fill("0.25");
  await page.getByRole("button", { name: "save", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: /saved/i })).toBeVisible();
  const calls = await (await request.get(`${apiOrigin}/__calls`)).json();
  const updates = calls.filter((call: { method: string }) => call.method === "PATCH");
  expect(updates).toHaveLength(1);
  expect(updates[0].body).toMatchObject({ min_match_score: 0.25, max_required_experience_years: null, work_auth: "Authorized", auto_submit: false });
});


test("application and tracker tables sort and filter without losing detail links or outcomes", async ({ page, request }) => {
  expect((await request.post(`${apiOrigin}/__table-fixtures`)).ok()).toBeTruthy();
  await page.goto("/applications");
  const pipeline = page.getByRole("table");
  await expect(pipeline.getByRole("link")).toHaveCount(2);
  await page.getByRole("button", { name: "Company / Role" }).click();
  await expect(pipeline.getByRole("link").first()).toHaveAttribute("href", "/applications/synthetic-second");
  await page.getByRole("button", { name: "Created" }).click();
  await expect(pipeline.getByRole("columnheader", { name: "Created" })).toHaveAttribute("aria-sort", "descending");
  await expect(pipeline.getByRole("link").first()).toHaveAttribute("href", "/applications/synthetic-application");
  await page.getByRole("searchbox", { name: "Search applications" }).fill("ALPHA");
  await expect(pipeline.getByRole("link")).toHaveCount(1);
  await page.getByRole("searchbox").fill("not-a-company");
  await expect(pipeline.getByText("No matching applications.")).toBeVisible();
  await page.getByRole("searchbox").clear();
  await expect(pipeline.getByRole("link")).toHaveCount(2);

  await page.goto("/tracker");
  const tracker = page.getByRole("table");
  const trackerRows = tracker.locator("tbody").getByRole("row");
  await page.getByRole("button", { name: "Match Score" }).click();
  await expect(trackerRows.first().getByRole("link", { name: "Software Engineer, Agent Tools", exact: true }))
    .toHaveAttribute("href", "/applications/synthetic-application");
  await expect(trackerRows.first().getByRole("link", { name: "Prepare for interview", exact: true }))
    .toHaveAttribute("href", "/applications/synthetic-application/interview");
  await expect(tracker.getByText("0.300", { exact: true })).toBeVisible();
  await expect(tracker.getByText("Not available", { exact: true })).toBeVisible();
  await page.getByRole("searchbox", { name: "Search tracker" }).fill("rejected");
  await expect(trackerRows).toHaveCount(1);
  await expect(trackerRows.first().getByRole("link", { name: "https://alpha.example/jobs/platform", exact: true }))
    .toHaveAttribute("href", "/applications/synthetic-second");
  await expect(trackerRows.first().getByRole("link", { name: "Prepare for interview", exact: true }))
    .toHaveAttribute("href", "/applications/synthetic-second/interview");
  await expect(page.getByRole("heading", { name: "Interview", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Rejection", exact: true })).toBeVisible();
});
