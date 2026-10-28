import { expect, test } from "@playwright/test";

const unique = (label: string) =>
  `${label} ${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

// Runs against the deterministic fake embedding provider (ARCHPILOT_EMBEDDING_PROVIDER=fake).
test("embeds with the fake provider, searches by vector and hybrid, and persists", async ({
  page,
}) => {
  await page.goto("/#/projects");
  await expect(page.getByText("Backend: ok")).toBeVisible();

  const name = unique("Vectors");
  await page.getByRole("button", { name: "New project" }).click();
  const dialog = page.getByRole("dialog", { name: "New project" });
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByRole("button", { name: "Create project" }).click();
  await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("button", { name: "Open documents" }).click();

  const factA = "quillmarrowalpha";
  const factB = "tessellbravobeta";
  const files = [
    {
      name: "ledger-notes.md",
      body: `# Ledger\n\nThe ${factA} ledger closes.\n`,
    },
    {
      name: "relay-notes.md",
      body: `# Relay\n\nThe ${factB} relay forwards.\n`,
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
  await expect(search.getByTestId("test-vector-label")).toContainText(
    /not a semantic model/,
  );
  await expect(search.getByTestId("embedding-identity")).toContainText(
    "fake / fake-hash-embedding",
  );

  for (const file of files) {
    await search
      .getByRole("button", { name: `Index ${file.name}`, exact: true })
      .click();
    await expect(
      search.getByRole("button", { name: `Reindex ${file.name}`, exact: true }),
    ).toBeVisible();
    await search
      .getByRole("button", { name: `Embed ${file.name}`, exact: true })
      .click();
    await expect(
      search.getByRole("button", {
        name: `Re-embed ${file.name}`,
        exact: true,
      }),
    ).toBeVisible();
  }

  await search.getByLabel("Search mode").selectOption("vector");
  await search.getByLabel("Search query").fill(factB);
  await search.getByRole("button", { name: "Search documents" }).click();
  const results = search.getByRole("region", { name: "Search results" });
  await expect(results.getByTestId("effective-mode")).toContainText(
    /Vector results \(development\/test vectors\)/,
  );
  await expect(
    results.getByText("relay-notes.md", { exact: true }),
  ).toBeVisible();
  await expect(
    results.getByText("ledger-notes.md", { exact: true }),
  ).toHaveCount(0);

  await search.getByLabel("Search mode").selectOption("hybrid");
  await search.getByRole("button", { name: "Search documents" }).click();
  await expect(results.getByTestId("effective-mode")).toContainText(/Hybrid/);
  await expect(results).toContainText(/keyword #1 \/ vector #1/);

  await page.reload();
  await expect(page.getByText("Backend: ok")).toBeVisible();
  const after = page.getByRole("region", { name: "Document search" });
  await expect(
    after.getByRole("button", { name: "Re-embed relay-notes.md", exact: true }),
  ).toBeVisible();
  await after.getByLabel("Search mode").selectOption("hybrid");
  await after.getByLabel("Search query").fill(factA);
  await after.getByRole("button", { name: "Search documents" }).click();
  await expect(
    after
      .getByRole("region", { name: "Search results" })
      .getByText("ledger-notes.md", { exact: true }),
  ).toBeVisible();
});
