import type { DiagramEdge, DiagramNode } from "./api";

const NODE_W = 150;
const NODE_H = 48;
const GAP_X = 50;
const GAP_Y = 80;
const PAD = 20;

/** Assigns each node a layer (longest path from a source); cycles are tolerated. */
function layers(nodes: DiagramNode[], edges: DiagramEdge[]) {
  const ids = new Set(nodes.map((n) => n.id));
  const valid = edges.filter((e) => ids.has(e.source) && ids.has(e.target));
  const layer = new Map(nodes.map((n) => [n.id, 0]));
  for (let i = 0; i < nodes.length; i++) {
    let changed = false;
    for (const e of valid) {
      const next = (layer.get(e.source) ?? 0) + 1;
      if (next > (layer.get(e.target) ?? 0) && next < nodes.length) {
        layer.set(e.target, next);
        changed = true;
      }
    }
    if (!changed) break;
  }
  return { layer, valid };
}

/**
 * Renders a validated component graph as SVG. Every label is a React text
 * node, so model output is never interpreted as markup or code.
 */
export default function ArchitectureDiagram({
  nodes,
  edges,
}: {
  nodes: DiagramNode[];
  edges: DiagramEdge[];
}) {
  if (nodes.length === 0) {
    return <p className="state">This proposal has no diagram nodes.</p>;
  }
  const { layer, valid } = layers(nodes, edges);
  const rows = new Map<number, DiagramNode[]>();
  for (const n of nodes) {
    const l = layer.get(n.id) ?? 0;
    rows.set(l, [...(rows.get(l) ?? []), n]);
  }
  const maxRow = Math.max(...[...rows.values()].map((r) => r.length));
  const width = PAD * 2 + maxRow * NODE_W + (maxRow - 1) * GAP_X;
  const height = PAD * 2 + rows.size * NODE_H + (rows.size - 1) * GAP_Y;
  const pos = new Map<string, { x: number; y: number }>();
  for (const [l, row] of rows) {
    const rowW = row.length * NODE_W + (row.length - 1) * GAP_X;
    const x0 = (width - rowW) / 2;
    row.forEach((n, i) =>
      pos.set(n.id, { x: x0 + i * (NODE_W + GAP_X), y: PAD + l * (NODE_H + GAP_Y) }),
    );
  }
  const names = new Map(nodes.map((n) => [n.id, n.name]));

  return (
    <div>
      <div className="diagram-wrap">
        <svg
          role="img"
          aria-label="Component diagram"
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
        >
          {valid.map((e, i) => {
            const a = pos.get(e.source)!;
            const b = pos.get(e.target)!;
            const x1 = a.x + NODE_W / 2;
            const y1 = a.y + NODE_H / 2;
            const x2 = b.x + NODE_W / 2;
            const y2 = b.y + NODE_H / 2;
            return (
              <g className="diagram-edge" key={i} data-testid="diagram-edge">
                <line x1={x1} y1={y1} x2={x2} y2={y2} />
                <text x={(x1 + x2) / 2} y={(y1 + y2) / 2} textAnchor="middle">
                  {e.relationship}
                </text>
              </g>
            );
          })}
          {nodes.map((n) => {
            const p = pos.get(n.id)!;
            return (
              <g className="diagram-node" key={n.id} data-testid="diagram-node">
                <rect x={p.x} y={p.y} width={NODE_W} height={NODE_H} rx={8} />
                <text x={p.x + NODE_W / 2} y={p.y + 20} textAnchor="middle">
                  {n.name}
                </text>
                <text
                  x={p.x + NODE_W / 2}
                  y={p.y + 36}
                  textAnchor="middle"
                  fontSize="10"
                >
                  {n.type}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <ul aria-label="Diagram relationships" className="muted">
        {valid.map((e, i) => (
          <li key={i}>
            {names.get(e.source)} {e.relationship} {names.get(e.target)}
          </li>
        ))}
      </ul>
    </div>
  );
}
