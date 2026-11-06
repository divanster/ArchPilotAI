import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import NextStepCard from "./NextStepCard";

afterEach(() => vi.unstubAllGlobals());

describe("NextStepCard", () => {
  it("shows one card with one primary action from the real project state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.includes("/requirements")
          ? new Response(JSON.stringify([{ id: "r1", status: "draft" }]), { status: 200 })
          : new Response("[]", { status: 200 }),
      ),
    );
    render(<NextStepCard projectId="p1" />);
    const card = await screen.findByRole("region", { name: "Next step" });
    expect(card.getAttribute("data-step")).toBe("confirm-requirements");
    const buttons = card.querySelectorAll("a.primary, button.primary");
    expect(buttons).toHaveLength(1);
    expect(buttons[0].textContent).toBe("Review requirements");
  });
});
