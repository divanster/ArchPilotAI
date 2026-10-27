import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProjectsPage from "./ProjectsPage";
import type { Project } from "./api";

// Documents have their own tests; keep these tests focused on project behaviour.
vi.mock("./DocumentsSection", () => ({ default: () => null }));

const base: Project = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "Alpha",
  description: "First",
  status: "draft",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

describe("ProjectsPage", () => {
  const fetchMock = vi.fn();

  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("shows loading then the project list", async () => {
    fetchMock.mockResolvedValueOnce(json([base]));
    render(<ProjectsPage />);
    expect(screen.getByText("Loading projects…")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Alpha" })).toBeInTheDocument();
  });

  it("shows an error and retries", async () => {
    fetchMock.mockRejectedValueOnce(new Error("Network down"));
    fetchMock.mockResolvedValueOnce(json([]));
    render(<ProjectsPage />);
    expect(await screen.findByText("Network down")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No projects yet.")).toBeInTheDocument();
  });

  it("creates a project", async () => {
    fetchMock.mockResolvedValueOnce(json([]));
    render(<ProjectsPage />);
    await screen.findByText("No projects yet.");

    fetchMock.mockResolvedValueOnce(json(base, 201));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "  Alpha " } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(await screen.findByRole("button", { name: "Alpha" })).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toMatch(/\/api\/projects$/);
    expect(JSON.parse(init.body)).toEqual({ name: "Alpha", description: null, status: "draft" });
  });

  it("validates the name before submitting", async () => {
    fetchMock.mockResolvedValueOnce(json([]));
    render(<ProjectsPage />);
    await screen.findByText("No projects yet.");
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByText("Name is required")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows server errors from create", async () => {
    fetchMock.mockResolvedValueOnce(json([]));
    render(<ProjectsPage />);
    await screen.findByText("No projects yet.");
    fetchMock.mockResolvedValueOnce(json({ detail: [{ msg: "String too long" }] }, 422));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByText("String too long")).toBeInTheDocument();
  });

  it("views and edits a project", async () => {
    fetchMock.mockResolvedValueOnce(json([base]));
    render(<ProjectsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Alpha" }));
    const details = screen.getByRole("region", { name: "Project details" });
    expect(within(details).getByText("Status: draft")).toBeInTheDocument();

    fireEvent.click(within(details).getByRole("button", { name: "Edit" }));
    const edit = screen.getByRole("region", { name: "Edit project" });
    fetchMock.mockResolvedValueOnce(json({ ...base, name: "Beta", status: "active" }));
    fireEvent.change(within(edit).getByLabelText("Name"), { target: { value: "Beta" } });
    fireEvent.change(within(edit).getByLabelText("Status"), { target: { value: "active" } });
    fireEvent.click(within(edit).getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("Status: active")).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toMatch(new RegExp(`/api/projects/${base.id}$`));
    expect(init.method).toBe("PATCH");
  });

  it("requires confirmation before deleting", async () => {
    fetchMock.mockResolvedValueOnce(json([base]));
    render(<ProjectsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Alpha" }));

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Keep project" }));
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204 } as Response);
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    await waitFor(() => expect(screen.getByText("No projects yet.")).toBeInTheDocument());
    expect(fetchMock.mock.calls[1][1].method).toBe("DELETE");
  });

  it("shows an error when delete fails", async () => {
    fetchMock.mockResolvedValueOnce(json([base]));
    render(<ProjectsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Alpha" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fetchMock.mockResolvedValueOnce(json({ detail: "Project not found" }, 404));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));
    expect(await screen.findByText("Project not found")).toBeInTheDocument();
  });
});
