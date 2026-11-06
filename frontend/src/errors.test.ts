import { afterEach, describe, expect, it, vi } from "vitest";
import { listProjects } from "./api";
import { describeFailure, looksLikeToken, redactSecrets } from "./errors";

afterEach(() => vi.unstubAllGlobals());

function reply(status: number, body: string, json = true) {
  return new Response(body, {
    status,
    headers: { "Content-Type": json ? "application/json" : "text/plain" },
  });
}

async function failure(): Promise<string> {
  try {
    await listProjects();
  } catch (e) {
    return (e as Error).message;
  }
  return "";
}

describe("real error messages", () => {
  it("uses the API's own message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => reply(409, JSON.stringify({ detail: "Name taken" }))));
    expect(await failure()).toBe("Name taken");
  });

  it("says the backend is not running when nothing answers", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    const msg = await failure();
    expect(msg).toMatch(/Cannot reach the backend/);
    expect(msg).not.toMatch(/^Failed to fetch$/);
  });

  it("points at CORS when the server answers a no-cors probe", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (init?.mode === "no-cors") return new Response(null, { status: 200 });
        throw new TypeError("Failed to fetch");
      }),
    );
    expect(await failure()).toMatch(/CORS/);
  });

  it("hints at a missing migration for a 500 that says does not exist", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => reply(500, 'relation "change_requests" does not exist', false)),
    );
    const msg = await failure();
    expect(msg).toMatch(/does not exist/);
    expect(msg).toMatch(/alembic upgrade head/);
  });

  it("hints at token problems for 401 and 403", () => {
    expect(describeFailure(401, "Bad credentials")).toMatch(/token/i);
    expect(describeFailure(403, "Forbidden")).toMatch(/permission/i);
  });

  it("never shows secrets", () => {
    const text = "push failed for https://user:hunter2pass@github.com/o/r token=ghp_abcdefghij1234567890";
    const safe = describeFailure(500, text);
    expect(safe).not.toContain("hunter2pass");
    expect(safe).not.toContain("ghp_abcdefghij1234567890");
    expect(redactSecrets("Authorization: Bearer abcdefghijklmnop")).not.toContain("abcdefghijklmnop");
  });
});

describe("looksLikeToken", () => {
  it("flags tokens and not URLs", () => {
    expect(looksLikeToken("ghp_abcdefghijklmnopqrstuvwxyz0123")).toBe(true);
    expect(looksLikeToken("github_pat_11ABCDEFG0abcdefghij")).toBe(true);
    expect(looksLikeToken("https://github.com/owner/repo")).toBe(false);
    expect(looksLikeToken("")).toBe(false);
  });
});
