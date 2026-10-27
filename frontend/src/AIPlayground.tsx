import { useState, type FormEvent } from "react";
import {
  AI_MAX_PROMPT_CHARS,
  generateAI,
  type AIGenerateResponse,
} from "./api";

function tokens(value: number | null): string {
  return value === null ? "not reported" : String(value);
}

export default function AIPlayground() {
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AIGenerateResponse | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (loading) return;
    if (!prompt.trim()) {
      setError("Enter a prompt.");
      return;
    }
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      setResult(await generateAI(prompt));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section aria-labelledby="ai-heading">
      <div className="page-header">
        <div>
          <h1 id="ai-heading">AI Playground</h1>
          <p>
            Local prototype. Use synthetic or public prompts only; never paste
            client data or secrets.
          </p>
        </div>
      </div>
      <div className="two-col">
        <form onSubmit={onSubmit} className="card">
          <div className="field">
            <label htmlFor="ai-prompt">Prompt</label>
            <textarea
              id="ai-prompt"
              rows={8}
              maxLength={AI_MAX_PROMPT_CHARS}
              value={prompt}
              placeholder="Describe what you want the model to do…"
              onChange={(e) => setPrompt(e.target.value)}
            />
            <span className="hint">
              {prompt.length} / {AI_MAX_PROMPT_CHARS} characters
            </span>
          </div>
          <button type="submit" disabled={loading}>
            {loading ? "Generating…" : "Generate"}
          </button>
          {error && <p role="alert">{error}</p>}
        </form>
        <div className="card">
          <h2>Response</h2>
          {!result && !loading && (
            <p className="muted">The model’s answer appears here.</p>
          )}
          {loading && (
            <div aria-busy="true">
              <div className="skeleton" aria-hidden="true" />
            </div>
          )}
          {result && (
            <div aria-label="AI result" className="stack">
              <pre>{result.answer}</pre>
              <dl className="meta-list">
                <div>
                  <dt>Provider</dt>
                  <dd>{result.provider}</dd>
                </div>
                <div>
                  <dt>Model</dt>
                  <dd>{result.model}</dd>
                </div>
                <div>
                  <dt>Latency</dt>
                  <dd>{result.latency_ms} ms</dd>
                </div>
                <div>
                  <dt>Input tokens</dt>
                  <dd>{tokens(result.usage.input_tokens)}</dd>
                </div>
                <div>
                  <dt>Output tokens</dt>
                  <dd>{tokens(result.usage.output_tokens)}</dd>
                </div>
                <div>
                  <dt>Total tokens</dt>
                  <dd>{tokens(result.usage.total_tokens)}</dd>
                </div>
              </dl>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
