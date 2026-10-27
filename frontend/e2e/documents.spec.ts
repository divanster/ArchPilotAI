import { expect, test } from "@playwright/test";

const unique = (label: string) => `${label} ${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

test("uploads a synthetic document and keeps it after refresh", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Backend: ok")).toBeVisible();

  const name = unique("Docs");
  await page.getByLabel("Name").fill(name);
  await page.getByRole("button", { name: "Create project" }).click();
  await page.getByRole("button", { name, exact: true }).click();

  const marker = unique("synthetic-marker");
  await page.getByLabel("Document file").setInputFiles({
    name: "synthetic-requirements.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(`# Synthetic requirements\n\n${marker}\n`),
  });
  await page.getByRole("button", { name: "Upload document" }).click();
  await expect(page.getByRole("button", { name: "synthetic-requirements.md" })).toBeVisible();

  await page.reload();
  await expect(page.getByText("Backend: ok")).toBeVisible();
  await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("button", { name: "synthetic-requirements.md" }).click();
  await expect(page.getByRole("group", { name: "Document preview" })).toContainText(marker);
});
