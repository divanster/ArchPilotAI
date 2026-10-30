import { useEffect, useState } from "react";
import {
  commitPatchToBranch,
  createPullRequest,
  getGitSettings,
  pushGitBranch,
  type GitCommitResult,
  type GitPullRequestResult,
  type GitSettings,
} from "./api";
import { hrefFor } from "./routes";
import { Modal } from "./ui";

const errText = (e: unknown, fallback: string) =>
  e instanceof Error ? e.message : fallback;

/** Commit, push and pull request actions for one accepted patch. Push is always a manual step. */
export default function GitPatchActions({
  projectId,
  patchId,
  reviewer,
}: {
  projectId: string;
  patchId: string;
  reviewer: string | null;
}) {
  const [settings, setSettings] = useState<GitSettings | null | undefined>(
    undefined,
  );
  const [commit, setCommit] = useState<GitCommitResult | null>(null);
  const [pushed, setPushed] = useState(false);
  const [pr, setPr] = useState<GitPullRequestResult | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    getGitSettings(projectId)
      .then((s) => live && setSettings(s))
      .catch(() => live && setSettings(null));
    return () => {
      live = false;
    };
  }, [projectId]);

  if (settings === undefined) return null;
  if (settings === null) {
    return (
      <p className="hint">
        No external repository is configured.{" "}
        <a href={hrefFor("repository", projectId)}>Open Repository settings</a>
      </p>
    );
  }

  const run = async (fn: () => Promise<void>, fallback: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errText(e, fallback));
    } finally {
      setBusy(false);
    }
  };

  const doCommit = () =>
    run(async () => {
      setCommit(await commitPatchToBranch(projectId, patchId));
      setPushed(false);
      setPr(null);
    }, "Commit failed");

  const doPush = () =>
    run(async () => {
      if (!commit) return;
      await pushGitBranch(projectId, commit.branch, reviewer || "reviewer");
      setPushed(true);
      setConfirming(false);
    }, "Push failed");

  const doPr = () =>
    run(
      async () => setPr(await createPullRequest(projectId, patchId)),
      "Pull request failed",
    );

  const hasToken = settings.pat.saved;

  return (
    <section aria-label="External repository">
      <strong>External repository</strong>
      <div className="btn-row">
        <button type="button" disabled={busy} onClick={() => void doCommit()}>
          Commit to branch
        </button>
        <button
          type="button"
          disabled={busy || !commit}
          onClick={() => setConfirming(true)}
        >
          Push branch
        </button>
        <button
          type="button"
          disabled={busy || !pushed}
          onClick={() => void doPr()}
        >
          {hasToken ? "Create pull request" : "Open compare link"}
        </button>
      </div>
      {commit && (
        <p role="status">
          Committed {commit.commit.slice(0, 8)} to {commit.branch} (
          {commit.files.length} file(s)).
        </p>
      )}
      {pushed && commit && <p role="status">Pushed {commit.branch}.</p>}
      {pr?.mode === "created" && pr.url && (
        <p role="status">
          Pull request created:{" "}
          <a href={pr.url} target="_blank" rel="noreferrer noopener">
            {pr.url}
          </a>
        </p>
      )}
      {pr?.mode === "compare_link" && (
        <p role="status">
          {pr.url ? (
            <a href={pr.url} target="_blank" rel="noreferrer noopener">
              Compare link for {pr.branch}
            </a>
          ) : (
            pr.message
          )}
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      {confirming && commit && (
        <Modal title="Push branch" onClose={() => setConfirming(false)}>
          <dl>
            <dt>Branch</dt>
            <dd>{commit.branch}</dd>
            <dt>Commits</dt>
            <dd>
              {commit.commit.slice(0, 8)} ({commit.files.length} file(s))
            </dd>
            <dt>Remote</dt>
            <dd>{settings.remote_url}</dd>
          </dl>
          <p>
            This sends the branch to the remote. It is never merged
            automatically.
          </p>
          <div className="row">
            <button
              type="button"
              className="primary"
              disabled={busy}
              onClick={() => void doPush()}
            >
              Confirm push
            </button>
            <button type="button" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        </Modal>
      )}
    </section>
  );
}
