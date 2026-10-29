import { useEffect, useState } from "react";
import {
  listProviders,
  testProvider,
  type ConnectionTestResult,
  type ProviderInfo,
} from "./api";
import { ErrorState, LoadingState, PageHeader } from "./ui";

const LABEL: Record<ProviderInfo["kind"], string> = {
  chat: "Chat",
  embeddings: "Embeddings",
};

export default function ProvidersPage() {
  const [providers, setProviders] = useState<ProviderInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, string>>({});
  const [running, setRunning] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    listProviders()
      .then((p) => live && setProviders(p))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, []);

  // A request is sent only from this click handler, never on load.
  const test = async (kind: ProviderInfo["kind"]) => {
    setRunning(kind);
    let message: string;
    try {
      const r: ConnectionTestResult = await testProvider(kind);
      message = `${r.ok ? "OK" : "Failed"}: ${r.message}`;
    } catch (e) {
      message = `Failed: ${(e as Error).message}`;
    }
    setResults((prev) => ({ ...prev, [kind]: message }));
    setRunning(null);
  };

  if (error) return <ErrorState message={error} />;
  if (!providers) return <LoadingState label="Loading providers" />;

  return (
    <>
      <PageHeader
        title="Providers"
        subtitle="Configured chat and embedding providers. Secrets are never shown."
      />
      <ul aria-label="Providers" className="stack">
        {providers.map((p) => (
          <li key={p.kind} className="card">
            <h2>{LABEL[p.kind]}</h2>
            <p>
              Provider: {p.provider}
              {p.model ? ` · model ${p.model}` : ""}
              {p.dimension ? ` · dimension ${p.dimension}` : ""}
            </p>
            <p>Status: {p.status}</p>
            {p.detail && <p>{p.detail}</p>}
            <button
              type="button"
              disabled={p.status !== "configured" || running !== null}
              onClick={() => void test(p.kind)}
              aria-label={`Test connection: ${LABEL[p.kind]}`}
            >
              Test connection
            </button>
            {results[p.kind] && <p role="status">{results[p.kind]}</p>}
          </li>
        ))}
      </ul>
    </>
  );
}
