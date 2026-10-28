import { expect, test } from "@playwright/test";

const unique = (label: string) =>
  `${label} ${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

test("indexes two documents, finds a unique fact in the right source, and persists", async ({
  page,
}) => {
  await page.goto("/#/projects");
  await expect(page.getByText("Backend: ok")).toBeVisible();

  const name = unique("Search");
  await page.getByRole("button", { name: "New project" }).click();
  const dialog = page.getByRole("dialog", { name: "New project" });
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByRole("button", { name: "Create project" }).click();
  await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("button", { name: "Open documents" }).click();

  const factA = "zephyrquartzalpha";
  const factB = "obsidianfluxbeta";
  const files = [
    {
      name: "billing-notes.md",
      body: `# Billing\n\nThe ${factA} module exports invoices.\n`,
    },
    {
      name: "scheduler-notes.md",
      body: `# Scheduler\n\nThe ${factB} service retries jobs.\n`,
    },
  ];
  for (const file of files) {
    await page.getByLabel("Document file").setInputFiles({
      name: file.name,
      mimeType: "text/markdown",
      buffer: Buffer.from(file.body),
    });
    await page.getByRole("button", { name: "Upload document" }).click();
    await expect(
      page.getByRole("button", { name: file.name, exact: true }),
    ).toBeVisible();
  }

  const search = page.getByRole("region", { name: "Document search" });
  await expect(search.getByText(/not an AI answer/)).toBeVisible();

  await search.getByRole("button", { name: "Index billing-notes.md" }).click();
  await expect(
    search.getByRole("button", { name: "Reindex billing-notes.md" }),
  ).toBeVisible();
  await search
    .getByRole("button", { name: "Index scheduler-notes.md" })
    .click();
  await expect(
    search.getByRole("button", { name: "Reindex scheduler-notes.md" }),
  ).toBeVisible();

  await search.getByLabel("Search query").fill(factB);
  await search.getByRole("button", { name: "Search documents" }).click();
  const results = search.getByRole("region", { name: "Search results" });
  await expect(
    results.getByText("scheduler-notes.md", { exact: true }),
  ).toBeVisible();
  await expect(
    results.getByText("billing-notes.md", { exact: true }),
  ).toHaveCount(0);
  await expect(results).toContainText(factB);
  await expect(results).toContainText(/characters \d+–\d+/);

  await results
    .getByRole("button", { name: "Open scheduler-notes.md preview" })
    .click();
  await expect(
    page.getByRole("group", { name: "Document preview" }),
  ).toContainText(factB);

  await page.reload();
  await expect(page.getByText("Backend: ok")).toBeVisible();
  const after = page.getByRole("region", { name: "Document search" });
  await expect(
    after.getByRole("button", { name: "Reindex billing-notes.md" }),
  ).toBeVisible();
  await after.getByLabel("Search query").fill(factA);
  await after.getByRole("button", { name: "Search documents" }).click();
  const again = after.getByRole("region", { name: "Search results" });
  await expect(
    again.getByText("billing-notes.md", { exact: true }),
  ).toBeVisible();
  await expect(
    again.getByText("scheduler-notes.md", { exact: true }),
  ).toHaveCount(0);
});
