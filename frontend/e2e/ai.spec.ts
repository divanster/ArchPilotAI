import { expect, test } from "@playwright/test";

test("generates an answer with the fake provider", async ({ page }) => {
  await page.goto("/#/playground");
  await expect(page.getByText("Backend: ok")).toBeVisible();

  const prompt = "Explain the benefits of a modular monolith.";
  await page.getByLabel("Prompt").fill(prompt);
  await page.getByRole("button", { name: "Generate" }).click();

  const result = page.getByLabel("AI result");
  await expect(result).toContainText(`Fake answer to: ${prompt}`);
  await expect(result.getByText("fake", { exact: true }).first()).toBeVisible();
});
