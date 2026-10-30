import { useCallback, useEffect, useId, useState, type FormEvent } from "react";
import {
  GIT_AUTH_METHODS,
  GIT_PROVIDERS,
  attachGitRepository,
  cloneGitRepository,
  getGitSettings,
  removeGitToken,
  saveGitSettings,
  saveGitToken,
  testGitConnection,
  type GitAuthMethod,
  type GitConnectionResult,
  type GitProvider,
  type GitSettings,
} from "./api";
import { ErrorState, LoadingState } from "./ui";

const errText = (e: unknown, fallback: string) =>
  e instanceof Error ? e.message : fallback;

interface Form {
  provider: GitProvider;
  remote_url: string;
  clone_folder: string;
  base_branch: string;
  branch_prefix: string;
  author_name: string;
  author_email: string;
  auth_method: GitAuthMethod;
}

const EMPTY: Form = {
  provider: "github",
  remote_url: "",
  clone_folder: "",
  base_branch: "main",
  branch_prefix: "agent/",
  author_name: "",
  author_email: "",
  auth_method: "credential_manager",
};

const toForm = (s: GitSettings): Form => ({
  provider: s.provider,
  remote_url: s.remote_url,
  clone_folder: s.clone_folder,
  base_branch: s.base_branch,
  branch_prefix: s.branch_prefix,
  author_name: s.author_name,
  author_email: s.author_email,
  auth_method: s.auth_method,
});

export default function RepositoryPage({ projectId }: { projectId: string }) {
  const uid = useId();
  const [settings, setSettings] = useState<GitSettings | null>(null);
  const [form, setForm] = useState<Form>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [token, setToken] = useState("");
  const [replacing, setReplacing] = useState(false);
  const [test, setTest] = useState<GitConnectionResult | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const s = await getGitSettings(projectId);
      setSettings(s);
      if (s) setForm(toForm(s));
    } catch (e) {
      setLoadError(errText(e, "Could not load repository settings"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (fn: () => Promise<void>, fallback: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      setError(errText(e, fallback));
    } finally {
      setBusy(false);
    }
  };

  const set = <K extends keyof Form>(key: K, value: Form[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const onSave = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const saved = await saveGitSettings(projectId, {
        ...form,
        remote_url: form.remote_url.trim(),
        clone_folder: form.clone_folder.trim() || null,
        base_branch: form.base_branch.trim(),
        branch_prefix: form.branch_prefix.trim(),
        author_name: form.author_name.trim(),
        author_email: form.author_email.trim(),
      });
      setSettings(saved);
      setForm(toForm(saved));
      setNotice("Repository settings saved.");
    }, "Could not save settings");
  };

  const onSaveToken = () =>
    void run(async () => {
      const value = token;
      // Clear the field before awaiting so the secret never lingers in the UI.
      setToken("");
      const saved = await saveGitToken(projectId, value);
      setSettings(saved);
      setReplacing(false);
      setNotice("Token saved.");
    }, "Could not save the token");

  const onRemoveToken = () =>
    void run(async () => {
      setSettings(await removeGitToken(projectId));
      setReplacing(false);
      setNotice("Token removed.");
    }, "Could not remove the token");

  const onTest = () =>
    void run(
      async () => setTest(await testGitConnection(projectId)),
      "Test failed",
    );

  const onClone = () =>
    void run(async () => {
      await cloneGitRepository(projectId);
      setSettings(await getGitSettings(projectId));
      setNotice("Repository cloned.");
    }, "Clone failed");

  const onAttach = () =>
    void run(async () => {
      await attachGitRepository(projectId);
      setSettings(await getGitSettings(projectId));
      setNotice("Existing clone attached.");
    }, "Attach failed");

  if (loading) return <LoadingState label="Loading repository settings" />;
  if (loadError)
    return <ErrorState message={loadError} onRetry={() => void load()} />;

  const id = (name: string) => `${uid}-${name}`;
  const pat = settings?.pat;
  const showTokenInput = !pat?.saved || replacing;

  return (
    <div className="stack">
      <form
        onSubmit={onSave}
        noValidate
        className="card"
        aria-label="Repository settings"
      >
        <div className="field">
          <label htmlFor={id("provider")}>Provider</label>
          <select
            id={id("provider")}
            value={form.provider}
            onChange={(e) => set("provider", e.target.value as GitProvider)}
          >
            {GIT_PROVIDERS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={id("url")}>Remote URL</label>
          <input
            id={id("url")}
            value={form.remote_url}
            placeholder="https://github.com/owner/repo"
            onChange={(e) => set("remote_url", e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={id("folder")}>Clone folder</label>
          <input
            id={id("folder")}
            value={form.clone_folder}
            placeholder="Default: git/<project id>"
            onChange={(e) => set("clone_folder", e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={id("base")}>Base branch</label>
          <input
            id={id("base")}
            value={form.base_branch}
            onChange={(e) => set("base_branch", e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={id("prefix")}>Branch prefix</label>
          <input
            id={id("prefix")}
            value={form.branch_prefix}
            onChange={(e) => set("branch_prefix", e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={id("author")}>Commit author name</label>
          <input
            id={id("author")}
            value={form.author_name}
            onChange={(e) => set("author_name", e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={id("email")}>Commit author email</label>
          <input
            id={id("email")}
            type="email"
            value={form.author_email}
            onChange={(e) => set("author_email", e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={id("auth")}>Authentication</label>
          <select
            id={id("auth")}
            value={form.auth_method}
            onChange={(e) =>
              set("auth_method", e.target.value as GitAuthMethod)
            }
          >
            {GIT_AUTH_METHODS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </div>
        <div className="row">
          <button type="submit" className="primary" disabled={busy}>
            Save settings
          </button>
        </div>
      </form>

      {settings && (
        <section className="card" aria-label="Personal access token">
          <h2>Personal access token</h2>
          <p className="muted">
            Use a token with the minimum scopes: repository contents and pull
            requests only. It is kept in the operating system credential store,
            never in the database, and is never shown again.
          </p>
          {pat?.saved && !replacing && (
            <p role="status">Token configured, ends with ...{pat.last4}</p>
          )}
          {showTokenInput && (
            <div className="field">
              <label htmlFor={id("token")}>Token</label>
              <input
                id={id("token")}
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
            </div>
          )}
          <div className="row">
            {showTokenInput && (
              <button
                type="button"
                disabled={busy || token.trim() === ""}
                onClick={onSaveToken}
              >
                {pat?.saved ? "Replace token" : "Save token"}
              </button>
            )}
            {pat?.saved && !replacing && (
              <button
                type="button"
                disabled={busy}
                onClick={() => setReplacing(true)}
              >
                Replace token
              </button>
            )}
            {pat?.saved && replacing && (
              <button
                type="button"
                onClick={() => {
                  setReplacing(false);
                  setToken("");
                }}
              >
                Cancel
              </button>
            )}
            {pat?.saved && (
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={onRemoveToken}
              >
                Remove token
              </button>
            )}
          </div>
        </section>
      )}

      {settings && (
        <section className="card" aria-label="Connection and clone">
          <h2>Status</h2>
          <p>
            Local clone: {settings.cloned ? "ready" : "not available"} (
            {settings.clone_folder})
          </p>
          <div className="row">
            <button type="button" disabled={busy} onClick={onTest}>
              Test connection
            </button>
            <button
              type="button"
              disabled={busy || settings.cloned}
              onClick={onClone}
            >
              Clone repository
            </button>
            <button type="button" disabled={busy} onClick={onAttach}>
              Attach existing clone
            </button>
          </div>
          {test && (
            <p role="status">
              {test.reachable ? "Reachable" : "Not reachable"}
              {test.api_checked
                ? test.api_ok
                  ? " · provider API OK"
                  : " · provider API failed"
                : ""}
              {`: ${test.message}`}
            </p>
          )}
        </section>
      )}

      {notice && <p role="status">{notice}</p>}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
