import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DecisionsCard from "./DecisionsCard";
import type { DecisionsOverview, ProjectDecision } from "./api";

const PID = "11111111-1111-1111-1111-111111111111";
const AREAS = ["backend", "frontend", "database", "hosting", "auth", "testing", "style"];

function row(over: Partial<ProjectDecision>): ProjectDecision {
  return {
    id: "d1",
    area: "backend",
    choice: "Django",
    reason: "skills",
    decided_by: "Ana",
    decided_at: "2026-01-01T00:00:00Z",
    superseded_by: null,
    ...over,
  };
}

function mock(state: DecisionsOverview) {
  const posts: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        posts.push(body);
        const old = state.current.find((d) => d.area === body.area);
        if (old) {
          old.superseded_by = "new";
          state.history.push(old);
          state.current = state.current.filter((d) => d !== old);
        }
        state.current.push(row({ id: "new", area: body.area, choice: body.choice, reason: body.reason, decided_by: body.name }));
        return { ok: true, status: 201, json: async () => ({}) } as Response;
      }
      return { ok: true, status: 200, json: async () => state } as Response;
    }),
  );
  return posts;
}

describe("DecisionsCard", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("lists areas, marks AI recommendations and pre-fills them", async () => {
    mock({
      areas: AREAS,
      current: [],
      history: [],
      suggestions: { backend: { choice: "FastAPI", layer: "Backend", label: "AI recommendation" } },
    });
    render(<DecisionsCard projectId={PID} />);
    const item = await screen.findByTestId("decision-backend");
    expect(within(item).getByText("AI recommendation: FastAPI")).toBeTruthy();
    expect(screen.getAllByText("No decision")).toHaveLength(7);
    fireEvent.click(within(item).getByRole("button", { name: /Decide/ }));
    expect((screen.getByLabelText("Choice for backend") as HTMLInputElement).value).toBe("FastAPI");
  });

  it("needs a reason and a name, and a change moves the old decision to the history", async () => {
    const posts = mock({
      areas: AREAS,
      current: [row({})],
      history: [],
      suggestions: {},
    });
    render(<DecisionsCard projectId={PID} />);
    const item = await screen.findByTestId("decision-backend");
    fireEvent.click(within(item).getByRole("button", { name: /Change/ }));
    const save = screen.getByRole("button", { name: "Save decision" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Choice for backend"), { target: { value: "FastAPI" } });
    fireEvent.change(screen.getByLabelText("Reason for backend"), { target: { value: "async" } });
    expect(save.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Name for backend"), { target: { value: "Bo" } });
    fireEvent.click(save);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ area: "backend", choice: "FastAPI", reason: "async", name: "Bo" });
    expect(await screen.findByText(/Django — skills \(Ana, superseded\)/)).toBeTruthy();
    expect(within(screen.getByTestId("decision-backend")).getByText(/FastAPI/)).toBeTruthy();
  });
});
