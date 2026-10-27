import { useState, type FormEvent } from "react";
import { AI_MAX_PROMPT_CHARS, generateAI, type AIGenerateResponse } from "./api";

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
      <h2 id="ai-heading">AI Playground</h2>
      <p>
        Local prototype. Use synthetic or public prompts only; never paste client data or secrets.
      </p>
      <form onSubmit={onSubmit}>
        <label htmlFor="ai-prompt">Prompt</label>
        <textarea
          id="ai-prompt"
          rows={4}
          maxLength={AI_MAX_PROMPT_CHARS}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
        />
        <button type="submit" disabled={loading}>
          {loading ? "Generating…" : "Generate"}
        </button>
      </form>
      {error && <p role="alert">{error}</p>}
      {result && (
        <div aria-label="AI result">
          <pre>{result.answer}</pre>
          <dl>
            <dt>Provider</dt>
            <dd>{result.provider}</dd>
            <dt>Model</dt>
            <dd>{result.model}</dd>
            <dt>Latency</dt>
            <dd>{result.latency_ms} ms</dd>
            <dt>Input tokens</dt>
            <dd>{tokens(result.usage.input_tokens)}</dd>
            <dt>Output tokens</dt>
            <dd>{tokens(result.usage.output_tokens)}</dd>
            <dt>Total tokens</dt>
            <dd>{tokens(result.usage.total_tokens)}</dd>
          </dl>
        </div>
      )}
    </section>
  );
}
