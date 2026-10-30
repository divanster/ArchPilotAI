import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GitPatchActions from "./GitPatchActions";
import RepositoryPage from "./RepositoryPage";

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

const settings = (over: Record<string, unknown> = {}) => ({
  provider: "github",
  remote_url: "https://github.com/acme/app",
  clone_folder: "git/p1",
  base_branch: "main",
  branch_prefix: "agent/",
  author_name: "Ada",
  author_email: "ada@example.com",
  auth_method: "pat",
  pat: { saved: false, provider: null, last4: null },
  cloned: true,
  updated_at: "2025-01-01T00:00:00Z",
  ...over,
});

const withPat = (last4 = "abcd") =>
  settings({ pat: { saved: true, provider: "github", last4 } });

const commitResult = {
  branch: "agent/T-1-add-login",
  commit: "0123456789abcdef",
  created_branch: true,
  files: ["a.py", "b.py"],
};

const calls = (m: ReturnType<typeof vi.fn>) =>
  m.mock.calls.map(([url, init]) => ({
    url: String(url),
    method: (init as RequestInit | undefined)?.method ?? "GET",
    body: (init as RequestInit | undefined)?.body as string | undefined,
  }));

describe("git repository UI", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("saves the repository form and sends nothing else on load", async () => {
    fetchMock.mockResolvedValueOnce(
      json({ detail: { code: "not_configured", message: "x" } }, 404),
    );
    render(<RepositoryPage projectId="p1" />);
    fireEvent.change(await screen.findByLabelText("Remote URL"), {
      target: { value: "https://github.com/acme/app" },
    });
    fireEvent.change(screen.getByLabelText("Provider"), {
      target: { value: "gitlab" },
    });
    fireEvent.change(screen.getByLabelText("Commit author name"), {
      target: { value: "Ada" },
    });
    fireEvent.change(screen.getByLabelText("Commit author email"), {
      target: { value: "ada@example.com" },
    });
    expect(screen.getByLabelText("Base branch")).toHaveValue("main");
    expect(screen.getByLabelText("Branch prefix")).toHaveValue("agent/");
    expect(screen.getByLabelText("Authentication")).toHaveValue(
      "credential_manager",
    );
    expect(calls(fetchMock)).toHaveLength(1);

    fetchMock.mockResolvedValueOnce(
      json(settings({ provider: "gitlab", auth_method: "credential_manager" })),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    expect(
      await screen.findByText("Repository settings saved."),
    ).toBeInTheDocument();
    const put = calls(fetchMock)[1];
    expect(put.method).toBe("PUT");
    expect(JSON.parse(put.body!)).toMatchObject({
      provider: "gitlab",
      remote_url: "https://github.com/acme/app",
      author_name: "Ada",
      clone_folder: null,
    });
    expect(
      screen.queryByRole("button", { name: "Test connection" }),
    ).toBeInTheDocument();
  });

  it("shows a server validation error", async () => {
    fetchMock.mockResolvedValueOnce(json(settings()));
    render(<RepositoryPage projectId="p1" />);
    await screen.findByLabelText("Remote URL");
    fetchMock.mockResolvedValueOnce(
      json({ detail: "remote URL must be HTTPS or SSH" }, 422),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "remote URL must be HTTPS or SSH",
    );
  });

  it("keeps the PAT write-only: set, replace and remove", async () => {
    fetchMock.mockResolvedValueOnce(json(settings()));
    render(<RepositoryPage projectId="p1" />);
    const input = await screen.findByLabelText("Token");
    expect(input).toHaveAttribute("type", "password");
    expect(
      screen.getByText(/repository contents and pull requests only/i),
    ).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "ghp_secretvalue1234" } });
    fetchMock.mockResolvedValueOnce(json(withPat("1234")));
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(
      await screen.findByText("Token configured, ends with ...1234"),
    ).toBeInTheDocument();
    const put = calls(fetchMock)[1];
    expect(put.url).toContain("/api/projects/p1/git/pat");
    expect(put.method).toBe("PUT");
    expect(JSON.parse(put.body!)).toEqual({ token: "ghp_secretvalue1234" });
    // The token is neither echoed back nor left in the page.
    expect(document.body.innerHTML).not.toContain("ghp_secretvalue1234");
    expect(screen.queryByLabelText("Token")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Replace token" }));
    const again = await screen.findByLabelText("Token");
    expect(again).toHaveValue("");
    fireEvent.change(again, { target: { value: "ghp_other0000wxyz" } });
    fetchMock.mockResolvedValueOnce(json(withPat("wxyz")));
    fireEvent.click(screen.getByRole("button", { name: "Replace token" }));
    expect(
      await screen.findByText("Token configured, ends with ...wxyz"),
    ).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain("ghp_other0000wxyz");

    fetchMock.mockResolvedValueOnce(json(settings()));
    fireEvent.click(screen.getByRole("button", { name: "Remove token" }));
    expect(await screen.findByLabelText("Token")).toHaveValue("");
    const del = calls(fetchMock).at(-1)!;
    expect(del.method).toBe("DELETE");
    expect(del.url).toContain("/git/pat");
  });

  it("tests the connection only on request", async () => {
    fetchMock.mockResolvedValueOnce(json(withPat()));
    render(<RepositoryPage projectId="p1" />);
    const button = await screen.findByRole("button", {
      name: "Test connection",
    });
    expect(calls(fetchMock)).toHaveLength(1);
    fetchMock.mockResolvedValueOnce(
      json({ reachable: true, api_checked: true, api_ok: true, message: "ok" }),
    );
    fireEvent.click(button);
    expect(
      await screen.findByText(/Reachable · provider API OK/),
    ).toBeInTheDocument();
    expect(calls(fetchMock)[1]).toMatchObject({ method: "POST" });
    expect(calls(fetchMock)[1].url).toContain("/git/test");
  });

  it("shows the CI workflow template on request and copies it", async () => {
    fetchMock.mockResolvedValueOnce(json(withPat()));
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<RepositoryPage projectId="p1" />);
    const show = await screen.findByRole("button", {
      name: "Show CI workflow template",
    });
    expect(calls(fetchMock)).toHaveLength(1); // nothing fetched until asked
    const content = "name: ArchPilot tests\non:\n  workflow_dispatch:\n";
    fetchMock.mockResolvedValueOnce(
      json({
        path: ".github/workflows/archpilot-tests.yml",
        workflow_file: "archpilot-tests.yml",
        artifact: "archpilot-junit",
        content,
      }),
    );
    fireEvent.click(show);
    const pre = await screen.findByLabelText("Workflow file");
    expect(pre.textContent).toBe(content);
    expect(
      screen.getByText(".github/workflows/archpilot-tests.yml"),
    ).toBeInTheDocument();
    expect(calls(fetchMock)[1]).toMatchObject({ method: "GET" });
    expect(calls(fetchMock)[1].url).toContain(
      "/api/agent/ci/workflow-template",
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy workflow" }));
    expect(
      await screen.findByText("Copied to the clipboard"),
    ).toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith(content);
    expect(calls(fetchMock)).toHaveLength(2); // copying never writes anywhere
  });

  it("offers the CI workflow only for GitHub repositories", async () => {
    fetchMock.mockResolvedValueOnce(json(settings({ provider: "gitlab" })));
    render(<RepositoryPage projectId="p1" />);
    await screen.findByLabelText("Remote URL");
    expect(
      screen.queryByRole("button", { name: "Show CI workflow template" }),
    ).not.toBeInTheDocument();
  });

  async function committed(settingsBody: unknown) {
    fetchMock.mockResolvedValueOnce(json(settingsBody));
    render(<GitPatchActions projectId="p1" patchId="pa1" reviewer="Rita" />);
    const commit = await screen.findByRole("button", {
      name: "Commit to branch",
    });
    expect(screen.getByRole("button", { name: "Push branch" })).toBeDisabled();
    fetchMock.mockResolvedValueOnce(json(commitResult));
    fireEvent.click(commit);
    await screen.findByText(/Committed 01234567 to agent\/T-1-add-login/);
  }

  it("asks for confirmation before pushing and shows branch, commits and remote", async () => {
    await committed(withPat());
    fireEvent.click(screen.getByRole("button", { name: "Push branch" }));
    const dialog = await screen.findByRole("dialog", { name: "Push branch" });
    expect(dialog).toHaveTextContent("agent/T-1-add-login");
    expect(dialog).toHaveTextContent("01234567");
    expect(dialog).toHaveTextContent("https://github.com/acme/app");
    expect(calls(fetchMock).some((c) => c.url.endsWith("/git/push"))).toBe(
      false,
    );

    // Cancel sends nothing.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(calls(fetchMock).some((c) => c.url.endsWith("/git/push"))).toBe(
      false,
    );

    fireEvent.click(screen.getByRole("button", { name: "Push branch" }));
    fetchMock.mockResolvedValueOnce(
      json({ branch: commitResult.branch, pushed: true }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm push" }),
    );
    await waitFor(() =>
      expect(calls(fetchMock).some((c) => c.url.endsWith("/git/push"))).toBe(
        true,
      ),
    );
    const push = calls(fetchMock).find((c) => c.url.endsWith("/git/push"))!;
    expect(JSON.parse(push.body!)).toEqual({
      branch: "agent/T-1-add-login",
      approved_by: "Rita",
      confirm: true,
    });
    expect(
      await screen.findByText("Pushed agent/T-1-add-login."),
    ).toBeInTheDocument();
  });

  it("creates a pull request through the API when a token is configured", async () => {
    await committed(withPat());
    fetchMock.mockResolvedValueOnce(
      json({ branch: commitResult.branch, pushed: true }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Push branch" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm push" }),
    );
    await screen.findByText(/Pushed/);
    expect(
      screen.queryByRole("button", { name: "Open compare link" }),
    ).not.toBeInTheDocument();
    fetchMock.mockResolvedValueOnce(
      json({
        mode: "created",
        url: "https://github.com/acme/app/pull/7",
        branch: commitResult.branch,
        base: "main",
        title: "T-1",
        message: "Pull request created.",
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Create pull request" }),
    );
    const link = await screen.findByRole("link", {
      name: "https://github.com/acme/app/pull/7",
    });
    expect(link).toHaveAttribute("href", "https://github.com/acme/app/pull/7");
    expect(calls(fetchMock).at(-1)!.url).toContain("/patches/pa1/pull-request");
  });

  it("falls back to a compare link without a token", async () => {
    await committed(settings());
    fetchMock.mockResolvedValueOnce(
      json({ branch: commitResult.branch, pushed: true }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Push branch" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Confirm push" }),
    );
    await screen.findByText(/Pushed/);
    expect(
      screen.queryByRole("button", { name: "Create pull request" }),
    ).not.toBeInTheDocument();
    fetchMock.mockResolvedValueOnce(
      json({
        mode: "compare_link",
        url: "https://github.com/acme/app/compare/main...agent/T-1-add-login",
        branch: commitResult.branch,
        base: "main",
        title: "T-1",
        message: "Open the link.",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Open compare link" }));
    expect(
      await screen.findByRole("link", { name: /Compare link for/ }),
    ).toHaveAttribute(
      "href",
      "https://github.com/acme/app/compare/main...agent/T-1-add-login",
    );
  });

  it("points to Repository settings when none are configured", async () => {
    fetchMock.mockResolvedValueOnce(
      json({ detail: { code: "not_configured", message: "x" } }, 404),
    );
    render(<GitPatchActions projectId="p1" patchId="pa1" reviewer={null} />);
    expect(
      await screen.findByRole("link", { name: "Open Repository settings" }),
    ).toHaveAttribute("href", "#/repository/p1");
    expect(
      screen.queryByRole("button", { name: "Commit to branch" }),
    ).not.toBeInTheDocument();
  });

  it("shows a refusal from the commit endpoint", async () => {
    fetchMock.mockResolvedValueOnce(json(withPat()));
    render(<GitPatchActions projectId="p1" patchId="pa1" reviewer="Rita" />);
    fetchMock.mockResolvedValueOnce(
      json(
        { detail: { code: "dirty_tree", message: "Working tree is dirty." } },
        409,
      ),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Commit to branch" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Working tree is dirty.",
    );
  });
});
