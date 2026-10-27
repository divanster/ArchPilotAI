import { expect, test, type Page } from "@playwright/test";

const unique = (label: string) =>
  `${label} ${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

async function createProject(page: Page, name: string, description?: string) {
  await page.getByRole("button", { name: "New project" }).click();
  const dialog = page.getByRole("dialog", { name: "New project" });
  await dialog.getByLabel("Name").fill(name);
  if (description) await dialog.getByLabel("Description").fill(description);
  await dialog.getByRole("button", { name: "Create project" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name, exact: true })).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await page.goto("/#/projects");
  await expect(page.getByText("Backend: ok")).toBeVisible();
});

test("creates a project and shows it in the list", async ({ page }) => {
  const name = unique("Create");
  await createProject(page, name, "Synthetic description");

  const item = page.getByRole("listitem").filter({ hasText: name });
  await expect(item).toContainText("draft");

  await page.reload();
  await expect(page.getByRole("button", { name, exact: true })).toBeVisible();
});

test("edits a project and keeps changes after refresh", async ({ page }) => {
  const name = unique("Edit");
  const renamed = `${name} renamed`;
  await createProject(page, name);

  await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  const form = page.getByRole("dialog", { name: "Edit project" });
  await form.getByLabel("Name").fill(renamed);
  await form.getByLabel("Description").fill("Updated description");
  await form.getByLabel("Status").selectOption("active");
  await form.getByRole("button", { name: "Save changes" }).click();

  const details = page.getByRole("region", { name: "Project details" });
  await expect(details.getByRole("heading", { name: renamed })).toBeVisible();

  await page.reload();
  await page.getByRole("button", { name: renamed, exact: true }).click();
  const reloaded = page.getByRole("region", { name: "Project details" });
  await expect(reloaded.getByText("active", { exact: true })).toBeVisible();
  await expect(reloaded.getByText("Updated description")).toBeVisible();
});

test("rejects an invalid name in the UI without calling the API", async ({
  page,
}) => {
  const posts: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST") posts.push(r.url());
  });

  await page.getByRole("button", { name: "New project" }).click();
  const dialog = page.getByRole("dialog", { name: "New project" });
  await dialog.getByRole("button", { name: "Create project" }).click();
  await expect(dialog.getByRole("alert")).toHaveText("Name is required");

  await dialog.getByLabel("Name").fill("   ");
  await dialog.getByRole("button", { name: "Create project" }).click();
  await expect(dialog.getByRole("alert")).toHaveText("Name is required");
  expect(posts).toHaveLength(0);
});

test("backend rejects an invalid name with 422", async ({ request }) => {
  const response = await request.post("http://localhost:8100/api/projects", {
    data: { name: "   " },
  });
  expect(response.status()).toBe(422);
});

test("cancels deletion, then confirms it", async ({ page }) => {
  const name = unique("Delete");
  await createProject(page, name);
  await page.getByRole("button", { name, exact: true }).click();

  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(
    page.getByRole("alertdialog", { name: "Confirm delete" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Keep project" }).click();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("button", { name, exact: true })).toBeVisible();

  await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByRole("button", { name: "Confirm delete" }).click();
  await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);

  await page.reload();
  await expect(page.getByText("Backend: ok")).toBeVisible();
  await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
});
