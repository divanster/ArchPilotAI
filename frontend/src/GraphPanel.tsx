import { useCallback, useEffect, useState } from "react";
import {
  getGraph,
  getGraphImpact,
  getGraphOrphans,
  rebuildGraph,
  type GraphData,
  type GraphImpact,
  type GraphNode,
  type ImpactMode,
} from "./api";

const KINDS = [
  "requirement",
  "component",
  "decision",
  "risk",
  "assumption",
  "open_question",
  "passage",
];

export default function GraphPanel({
  projectId,
  proposalId,
}: {
  projectId: string;
  proposalId: string;
}) {
  const [graph, setGraph] = useState<GraphData | null>(null);
  const [orphans, setOrphans] = useState<GraphNode[]>([]);
  const [impact, setImpact] = useState<GraphImpact | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [mode, setMode] = useState<ImpactMode>("direct");
  const [skipped, setSkipped] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const g = await getGraph(projectId, proposalId);
      setGraph(g);
      setOrphans(g.built ? (await getGraphOrphans(projectId, proposalId)).orphans : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the graph");
    }
  }, [projectId, proposalId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function build() {
    setBusy(true);
    setError(null);
    try {
      const summary = await rebuildGraph(projectId, proposalId);
      setSkipped(summary.skipped_requirement_refs);
      setSelected(null);
      setImpact(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Build failed");
    } finally {
      setBusy(false);
    }
  }

  async function select(nodeId: string, nextMode: ImpactMode = "direct") {
    setSelected(nodeId);
    setMode(nextMode);
    setImpact(null);
    setError(null);
    try {
      setImpact(await getGraphImpact(projectId, proposalId, nodeId, nextMode));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Impact query failed");
    }
  }

  if (!graph) {
    return (
      <div className="card" data-testid="graph-panel">
        {error ? <p role="alert">{error}</p> : <p className="muted">Loading graph…</p>}
      </div>
    );
  }

  const nodes = new Map(graph.nodes.map((n) => [n.id, n]));
  const label = (id: string) => nodes.get(id)?.label ?? id;

  return (
    <div className="card stack" data-testid="graph-panel">
      <h3>Traceability graph</h3>
      <p className="hint">
        Derived from the stored proposal and the confirmed requirements. It is rebuilt only on
        request or when the proposal is approved, so it can lag later changes.
      </p>
      {error && <p role="alert">{error}</p>}
      {!graph.built ? (
        <>
          <p>Graph not built</p>
          <div className="row">
            <button type="button" className="primary" disabled={busy} onClick={() => void build()}>
              Build graph
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="row">
            <span className="muted">
              {graph.node_count} nodes · {graph.edge_count} edges
              {graph.built_at ? ` · built ${graph.built_at}` : ""}
            </span>
            <button type="button" disabled={busy} onClick={() => void build()}>
              Rebuild graph
            </button>
          </div>
          {graph.truncated && <p role="status">Large graph: only the first part is shown.</p>}
          {skipped !== null && skipped > 0 && (
            <p role="status">
              {skipped} reference(s) to requirements that are no longer confirmed were left out.
            </p>
          )}

          <h4>Requirements without a component or decision</h4>
          {orphans.length === 0 ? (
            <p className="muted">None.</p>
          ) : (
            <ul aria-label="Orphan requirements">
              {orphans.map((o) => (
                <li key={o.id}>{o.label}</li>
              ))}
            </ul>
          )}

          <h4>Nodes</h4>
          {KINDS.map((kind) => {
            const list = graph.nodes.filter((n) => n.kind === kind);
            if (list.length === 0) return null;
            return (
              <section key={kind} aria-label={`Nodes: ${kind}`}>
                <h5>
                  {kind.replace("_", " ")} ({list.length})
                </h5>
                <ul>
                  {list.map((n) => (
                    <li key={n.id}>
                      <button
                        type="button"
                        aria-pressed={selected === n.id}
                        onClick={() => void select(n.id)}
                      >
                        {n.label}
                      </button>{" "}
                      <span className="muted">{n.source_ref}</span>
                    </li>
                  ))}
                </ul>
              </section>
            );
          })}

          {impact && (
            <section aria-label="Impact">
              <h4>Impact of {impact.root.label}</h4>
              <div className="row" role="group" aria-label="Impact mode">
                {(["direct", "transitive"] as ImpactMode[]).map((m) => (
                  <button
                    key={m}
                    type="button"
                    aria-pressed={mode === m}
                    onClick={() => selected && void select(selected, m)}
                  >
                    {m === "direct" ? "Direct" : "Transitive"}
                  </button>
                ))}
              </div>
              <p className="hint">
                What depends on this node, up to depth {impact.max_depth}. Paths can pass through a
                shared requirement, so check each link before relying on it.
              </p>
              {impact.items.length === 0 ? (
                <p className="muted">Nothing depends on this node.</p>
              ) : (
                <ul aria-label="Impacted nodes">
                  {impact.items.map((i) => (
                    <li key={i.node.id}>
                      [{i.node.kind}] {i.node.label} — depth {i.depth}, {i.via_kind} →{" "}
                      {label(i.reached_from)} (from {i.via_evidence})
                      {i.via_hub && " — via a shared hub"}
                    </li>
                  ))}
                </ul>
              )}
              {impact.hubs_not_expanded.length > 0 && (
                <ul aria-label="Hubs not expanded">
                  {impact.hubs_not_expanded.map((h) => (
                    <li key={h.node.id}>
                      {h.node.label} — not expanded: shared by {h.degree} items
                    </li>
                  ))}
                </ul>
              )}
              {impact.truncated && <p role="status">More dependents exist beyond the limit.</p>}
            </section>
          )}

          <h4>Edges</h4>
          <ul aria-label="Edges">
            {graph.edges.map((e) => (
              <li key={e.id}>
                {label(e.source_id)} —{e.kind}→ {label(e.target_id)}{" "}
                <span className="muted">(source field: {e.evidence})</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
