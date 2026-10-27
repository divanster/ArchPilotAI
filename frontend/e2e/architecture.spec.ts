import { expect, test } from "@playwright/test";

const unique = (label: string) =>
  `${label} ${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

test("generates architecture from confirmed requirements and persists review", async ({
  page,
}) => {
  await page.goto("/#/projects");
  await expect(page.getByText("Backend: ok")).toBeVisible();

  const name = unique("Arch");
  await page.getByRole("button", { name: "New project" }).click();
  const dialog = page.getByRole("dialog", { name: "New project" });
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByRole("button", { name: "Create project" }).click();
  await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("button", { name: "Open documents" }).click();
  await page.getByLabel("Document file").setInputFiles({
    name: "arch-spec.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# Synthetic spec\n\nThe system shall export reports as PDF.\nUsers must authenticate before accessing data.\n",
    ),
  });
  await page.getByRole("button", { name: "Upload document" }).click();
  await expect(page.getByRole("button", { name: "arch-spec.md" })).toBeVisible();

  await page.getByRole("link", { name: /Requirements/ }).click();
  await page.getByLabel("Project", { exact: true }).selectOption({ label: name });
  const requirements = page.getByRole("region", { name: "Requirements" });
  await requirements
    .getByLabel("Document to extract from")
    .selectOption({ label: "arch-spec.md" });
  await requirements.getByRole("button", { name: "Extract requirements" }).click();
  const items = requirements.getByRole("listitem");
  await expect(items).toHaveCount(2);
  await items.nth(0).getByRole("button", { name: "Confirm" }).click();
  await expect(items.nth(0).getByTestId("requirement-status")).toHaveText("confirmed");

  await page.getByRole("link", { name: /Architecture/ }).click();
  await page.getByLabel("Project", { exact: true }).selectOption({ label: name });
  const arch = page.getByRole("region", { name: "Architecture" });
  await expect(arch.getByText("No architecture proposals yet.")).toBeVisible();
  await arch.getByRole("button", { name: "Generate architecture" }).click();

  await expect(arch.getByTestId("proposal-status")).toHaveText("draft");
  await expect(arch.getByRole("img", { name: "Component diagram" })).toBeVisible();
  await expect(arch.getByTestId("diagram-node").first()).toBeVisible();

  await arch.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(arch.getByTestId("proposal-status")).toHaveText("approved");

  await page.reload();
  await expect(page.getByText("Backend: ok")).toBeVisible();
  const after = page.getByRole("region", { name: "Architecture" });
  await expect(after.getByTestId("proposal-status")).toHaveText("approved");
  await expect(after.getByRole("button", { name: "Approve", exact: true })).toBeDisabled();
});
