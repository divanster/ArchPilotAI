import { useId } from "react";
import type { DiagramEdge, DiagramNode } from "./api";

export const NODE_W = 160;
export const NODE_H = 52;
const GAP_X = 90;
const GAP_Y = 110;
const PAD = 30;
const LABEL_H = 18;
const CHAR_W = 6.4;
const LANE_OFFSET = 10;

interface Point {
  x: number;
  y: number;
}

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface PlacedEdge {
  edge: DiagramEdge;
  from: Point;
  to: Point;
  label: Box;
}

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

/** Where the ray from a box centre towards `toward` leaves the box. */
function borderPoint(center: Point, toward: Point): Point {
  const dx = toward.x - center.x;
  const dy = toward.y - center.y;
  if (dx === 0 && dy === 0) return center;
  const t = Math.min(
    dx === 0 ? Infinity : NODE_W / 2 / Math.abs(dx),
    dy === 0 ? Infinity : NODE_H / 2 / Math.abs(dy),
  );
  return { x: center.x + dx * t, y: center.y + dy * t };
}

export function overlaps(a: Box, b: Box, margin = 2): boolean {
  return (
    a.x < b.x + b.w + margin &&
    b.x < a.x + a.w + margin &&
    a.y < b.y + b.h + margin &&
    b.y < a.y + a.h + margin
  );
}

const POSITIONS = Array.from({ length: 17 }, (_, i) => 0.1 + i * 0.05).sort(
  (a, b) => Math.abs(a - 0.5) - Math.abs(b - 0.5),
);

/**
 * Positions edges and their labels. Edges run border to border (never through the
 * node centres), opposite edges between the same two nodes get separate lanes, and each
 * label takes the first spot along its edge that touches no node and no earlier label.
 */
export function placeEdges(
  edges: DiagramEdge[],
  pos: Map<string, Point>,
  width: number,
): PlacedEdge[] {
  const nodeBoxes: Box[] = [...pos.values()].map((p) => ({
    x: p.x,
    y: p.y,
    w: NODE_W,
    h: NODE_H,
  }));
  const taken: Box[] = [];
  const keys = new Set(edges.map((e) => `${e.source}>${e.target}`));
  return edges.map((edge) => {
    const a = pos.get(edge.source)!;
    const b = pos.get(edge.target)!;
    const ca = { x: a.x + NODE_W / 2, y: a.y + NODE_H / 2 };
    const cb = { x: b.x + NODE_W / 2, y: b.y + NODE_H / 2 };
    let sa = ca;
    let sb = cb;
    if (keys.has(`${edge.target}>${edge.source}`)) {
      // Opposite edges leave on opposite sides of the line so they never coincide.
      const len = Math.hypot(cb.x - ca.x, cb.y - ca.y) || 1;
      const side = edge.source < edge.target ? 1 : -1;
      const nx = (-(cb.y - ca.y) / len) * LANE_OFFSET * side;
      const ny = ((cb.x - ca.x) / len) * LANE_OFFSET * side;
      sa = { x: ca.x + nx, y: ca.y + ny };
      sb = { x: cb.x + nx, y: cb.y + ny };
    }
    const from = borderPoint(sa, sb);
    const to = borderPoint(sb, sa);
    const w = edge.relationship.length * CHAR_W + 12;
    const at = (t: number, dy = 0): Box => {
      const x = from.x + (to.x - from.x) * t - w / 2;
      return {
        x: Math.min(Math.max(x, 2), Math.max(2, width - w - 2)),
        y: from.y + (to.y - from.y) * t - LABEL_H / 2 + dy,
        w,
        h: LABEL_H,
      };
    };
    const free = (box: Box) =>
      !nodeBoxes.some((n) => overlaps(box, n)) && !taken.some((t) => overlaps(box, t));
    const label =
      POSITIONS.map((t) => at(t)).find(free) ??
      // Crowded: step the label off its edge until a gap is found.
      POSITIONS.flatMap((t) => [-1, 1, -2, 2].map((k) => at(t, k * LABEL_H))).find(free) ??
      at(0.5);
    taken.push(label);
    return { edge, from, to, label };
  });
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
  const markerId = `arrow-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
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
  const pos = new Map<string, Point>();
  for (const [l, row] of rows) {
    const rowW = row.length * NODE_W + (row.length - 1) * GAP_X;
    const x0 = (width - rowW) / 2;
    row.forEach((n, i) =>
      pos.set(n.id, { x: x0 + i * (NODE_W + GAP_X), y: PAD + l * (NODE_H + GAP_Y) }),
    );
  }
  const names = new Map(nodes.map((n) => [n.id, n.name]));
  const placed = placeEdges(valid, pos, width);

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
          <defs>
            <marker
              id={markerId}
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="8"
              markerHeight="8"
              orient="auto"
            >
              <path className="diagram-arrow" d="M0,0 L10,5 L0,10 z" />
            </marker>
          </defs>
          {placed.map(({ edge, from, to, label }, i) => (
            <g className="diagram-edge" key={i} data-testid="diagram-edge">
              <line
                x1={from.x}
                y1={from.y}
                x2={to.x}
                y2={to.y}
                markerEnd={`url(#${markerId})`}
              />
              <rect
                className="edge-label-bg"
                x={label.x}
                y={label.y}
                width={label.w}
                height={label.h}
                rx={4}
              />
              <text x={label.x + label.w / 2} y={label.y + label.h / 2 + 4} textAnchor="middle">
                {edge.relationship}
              </text>
            </g>
          ))}
          {nodes.map((n) => {
            const p = pos.get(n.id)!;
            return (
              <g className="diagram-node" key={n.id} data-testid="diagram-node">
                <rect x={p.x} y={p.y} width={NODE_W} height={NODE_H} rx={8} />
                <text x={p.x + NODE_W / 2} y={p.y + 22} textAnchor="middle">
                  {n.name}
                </text>
                <text x={p.x + NODE_W / 2} y={p.y + 40} textAnchor="middle" fontSize="10">
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
