import { test as base, expect } from "@playwright/test";

// A recurring gap's count opens onto the jobs behind it (`gap-evidence.tsx`).
// The answer stays short; Evidence details holds the jobs, each with its own
// requirement line, the projects declaring the skill, and the rest of the N
// read by the answer's own ids. The mock API's fixture is 37 jobs.
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
const ids = Array.from({ length: 37 }, (_, n) => `00000000-0000-4000-9000-${String(n).padStart(12, "0")}`);

test.beforeEach(async ({ request }) => {
  expect((await request.post(`${apiOrigin}/__reset`)).ok()).toBeTruthy();
});

test("a recurring gap opens onto every job behind its count", async ({ page, request }) => {
  await page.goto("/chat");
  await page.getByPlaceholder("Ask about your applications or open postings…").fill(
    "Which skill gaps recur across my strongest matches?",
  );
  await page.getByRole("button", { name: "Send" }).click();

  // The answer itself stays a line per gap; the jobs wait under Evidence details.
  await expect(page.getByText("— directly required by 37 of 912 open IT jobs", { exact: false })).toBeVisible();
  await expect(page.getByText("Kubernetes is required for platform 1", { exact: true })).toBeHidden();

  await page.getByText("Evidence details", { exact: true }).click();
  const gap = page.getByRole("region", { name: "Kubernetes: the jobs behind the count" });
  await expect(gap).toContainText("directly required by 37 of 912 jobs reviewed");
  await expect(gap).toContainText("Showing 10 of 37 jobs");
  await expect(gap.getByRole("listitem").filter({ hasText: "Platform Engineer 10" })).toContainText(
    "Kubernetes is required for platform 10",
  );
  await expect(gap.getByRole("link", { name: "example/infra" })).toHaveAttribute(
    "href",
    "https://github.com/example/infra/tree/0123456789abcdef0123456789abcdef01234567",
  );
  await expect(gap).toContainText("Declared in a dependency file · checked live at commit 0123456789");
  await expect(gap).not.toContainText(/implemented/i);
  await expect(page.getByText("confirm your contribution in Resume Studio", { exact: false })).toBeVisible();

  await gap.getByRole("button", { name: "Show next 25 · 27 more" }).click();
  await expect(gap).toContainText("Showing 35 of 37 jobs");
  await expect(gap.getByRole("listitem").filter({ hasText: "Platform Engineer 13" })).toContainText(
    "closed since this answer",
  );
  await gap.getByRole("button", { name: "Show next 2 · 2 more" }).click();
  await expect(gap).toContainText("All 37 jobs shown");
  await expect(gap.getByRole("button", { name: /Show/ })).toHaveCount(0);

  // Pages are the answer's own ids, in order: nothing searched again.
  const calls = (await (await request.get(`${apiOrigin}/__calls`)).json()) as Array<{
    path: string;
    body: { skill: string; posting_ids: string[] };
  }>;
  const pages = calls.filter((call) => call.path === "/chat/gap-jobs").map((call) => call.body);
  expect(pages).toEqual([
    { skill: "kubernetes", posting_ids: ids.slice(10, 35) },
    { skill: "kubernetes", posting_ids: ids.slice(35) },
  ]);
  // The drill-down never travels back as history.
  const asked = calls.filter((call) => call.path === "/chat/stream");
  expect(JSON.stringify(asked)).not.toContain("example/infra");
});

test("a chat saved before drill-downs still opens with its evidence text", async ({ page }) => {
  await page.addInitScript(() => {
    if (window.localStorage.getItem("jobrunner.chat.recents.v1")) return;
    window.localStorage.setItem(
      "jobrunner.chat.recents.v1",
      JSON.stringify([
        {
          id: "older-chat",
          title: "Which skill gaps recur across my strongest matches?",
          updatedAt: "2026-11-12T20:00:00.000Z",
          applicationId: null,
          turns: [
            { role: "you", text: "Which skill gaps recur across my strongest matches?" },
            {
              role: "assistant",
              text: "**Kubernetes** — directly required by 4 of 100 reviewed IT jobs. [P1]",
              provider: "ollama",
              local: true,
              mailInContext: false,
              searched: 100,
              unsearchable: 0,
              postingsScope: "top_matches",
              retrievalMethod: "graph",
              retrievalDetails: "P1: Platform Engineer at Example Cloud: Kubernetes: directly required by 4 of 100.",
              sources: [
                {
                  label: "P1",
                  posting_id: "00000000-0000-4000-9000-000000000000",
                  title: "Platform Engineer",
                  company: "Example Cloud",
                  location: "Remote, US",
                  url: "https://careers.example.com/jobs/platform",
                  cited: true,
                  locations: [],
                },
              ],
            },
          ],
        },
      ]),
    );
  });
  await page.goto("/chat");
  await page.getByRole("button", { name: /Recents/ }).click();
  await page.getByRole("button", { name: /^Which skill gaps recur/ }).click();

  await expect(page.getByText("directly required by 4 of 100 reviewed IT jobs", { exact: false }).first()).toBeVisible();
  await page.getByText("Evidence details", { exact: true }).click();
  await expect(page.getByText("P1: Platform Engineer at Example Cloud", { exact: false })).toBeVisible();
  await expect(page.getByRole("region", { name: /the jobs behind the count/ })).toHaveCount(0);
});
