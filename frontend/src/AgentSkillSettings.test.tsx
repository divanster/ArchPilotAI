import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AgentSkillSettings from "./AgentSkillSettings";
import type { AgentSkill, AgentSkillList } from "./api";

function skill(over: Partial<AgentSkill> = {}): AgentSkill {
  return {
    name: "code-review",
    description: "Review along two axes.",
    source: "vendored",
    overrides: null,
    license: "MIT",
    version: "4ae2317",
    content_hash: "a".repeat(64),
    enabled: true,
    file_count: 1,
    ...over,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setup(list: AgentSkillList, disabled = false) {
  let current = list;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      const enabled = JSON.parse(String(init.body)).enabled as boolean;
      const name = decodeURIComponent(url.split("/").pop() ?? "");
      current = {
        ...current,
        skills: current.skills.map((s) =>
          s.name === name ? { ...s, enabled } : s,
        ),
      };
      return json(current);
    }
    if (url.endsWith("/agent-skills")) return json(current);
    const name = decodeURIComponent(url.split("/").pop() ?? "");
    const found = current.skills.find((s) => s.name === name);
    return json({ ...found, body: `Body of ${name}`, files: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<AgentSkillSettings projectId="p" disabled={disabled} />);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe("AgentSkillSettings", () => {
  it("lists source, license, version and hash, and labels overrides", async () => {
    setup({
      skills: [
        skill(),
        skill({
          name: "mine",
          source: "project",
          overrides: "built-in",
          license: "",
          version: "2",
          content_hash: "b".repeat(64),
        }),
      ],
      problems: [
        { name: "bad", source: "project", code: "x", message: "Broken skill" },
      ],
    });
    expect(await screen.findByText("code-review")).toBeTruthy();
    expect(screen.getByText("vendored")).toBeTruthy();
    expect(screen.getByText(/license MIT/)).toBeTruthy();
    expect(screen.getByText("aaaaaaaaaaaa")).toBeTruthy();
    expect(screen.getByText("project")).toBeTruthy();
    expect(screen.getByText("overrides built-in")).toBeTruthy();
    expect(screen.getByText(/Not loaded: bad/)).toBeTruthy();
  });

  it("shows the body read-only and toggles a skill", async () => {
    const fetchMock = setup({ skills: [skill()], problems: [] });
    const view = await screen.findByRole("button", { name: "View" });
    await act(async () => {
      fireEvent.click(view);
    });
    const body = (await screen.findByLabelText(
      "Body of skill code-review",
    )) as HTMLTextAreaElement;
    expect(body.value).toBe("Body of code-review");
    expect(body.readOnly).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.queryByLabelText("Body of skill code-review")).toBeNull();

    const box = screen.getByRole("checkbox") as HTMLInputElement;
    expect(box.checked).toBe(true);
    await act(async () => {
      fireEvent.click(box);
    });
    await vi.waitFor(() =>
      expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
        false,
      ),
    );
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(put?.[0]).toContain("/agent-skills/code-review");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({ enabled: false });
  });

  it("locks the switches while a run is going", async () => {
    setup({ skills: [skill()], problems: [] }, true);
    const box = (await screen.findByRole("checkbox")) as HTMLInputElement;
    expect(box.disabled).toBe(true);
  });
});
