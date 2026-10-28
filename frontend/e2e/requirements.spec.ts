import { expect, test } from "@playwright/test";

const unique = (label: string) =>
  `${label} ${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

test("extracts requirements with the fake provider and persists human review", async ({
  page,
}) => {
  await page.goto("/#/projects");
  await expect(page.getByText("Backend: ok")).toBeVisible();

  const name = unique("Reqs");
  await page.getByRole("button", { name: "New project" }).click();
  const dialog = page.getByRole("dialog", { name: "New project" });
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByRole("button", { name: "Create project" }).click();
  await page.getByRole("button", { name, exact: true }).click();
  await page.getByRole("button", { name: "Open documents" }).click();
  const first = "The system shall export reports as PDF.";
  const second = "Users must authenticate before accessing data.";
  await page.getByLabel("Document file").setInputFiles({
    name: "synthetic-spec.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      `# Synthetic spec\n\nIntro text.\n\n${first}\n${second}\n`,
    ),
  });
  await page.getByRole("button", { name: "Upload document" }).click();
  await expect(
    page.getByRole("button", { name: "synthetic-spec.md", exact: true }),
  ).toBeVisible();

  await page.getByRole("link", { name: /Requirements/ }).click();
  await page
    .getByLabel("Project", { exact: true })
    .selectOption({ label: name });
  const requirements = page.getByRole("region", { name: "Requirements" });
  await requirements
    .getByLabel("Document to extract from")
    .selectOption({ label: "synthetic-spec.md" });
  await requirements
    .getByRole("button", { name: "Extract requirements" })
    .click();

  const items = requirements.getByRole("listitem");
  await expect(items).toHaveCount(2);
  await expect(requirements.getByLabel("Source quote").first()).toBeVisible();
  await expect(items.first().getByTestId("requirement-status")).toHaveText(
    "proposed",
  );

  await items.nth(0).getByRole("button", { name: "Confirm" }).click();
  await expect(items.nth(0).getByTestId("requirement-status")).toHaveText(
    "confirmed",
  );
  await items.nth(1).getByRole("button", { name: "Reject" }).click();
  await expect(items.nth(1).getByTestId("requirement-status")).toHaveText(
    "rejected",
  );

  await page.reload();
  await expect(page.getByText("Backend: ok")).toBeVisible();
  const after = page
    .getByRole("region", { name: "Requirements" })
    .getByRole("listitem");
  await expect(after).toHaveCount(2);
  const statuses = await after
    .getByTestId("requirement-status")
    .allTextContents();
  expect(statuses.sort()).toEqual(["confirmed", "rejected"]);
});
