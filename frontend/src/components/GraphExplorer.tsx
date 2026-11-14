import { useEffect, useRef } from "react";
import cytoscape from "cytoscape";
import dagre from "cytoscape-dagre";
import type { GraphEdge, GraphNode, StatusName } from "../types";

cytoscape.use(dagre);

/** Labels come from the curated file. Ids are the fallback for fixtures. */
function displayLabel(data: GraphNode["data"]): string {
  if (data.kind === "justification") return data.label;
  if (/^(SYN_|W\d|CL_)/.test(data.label)) {
    return data.label.replace(/^SYN_/, "").replace(/_/g, " ").toLowerCase();
  }
  return data.label;
}

const STATUS_COLOR: Record<StatusName, string> = {
  SUPPORTED: "#3fb950",
  SUPPORTED_WEAKENED: "#d29922",
  UNSUPPORTED: "#f85149",
  UNSUPPORTED_CIRCULAR: "#f85149",
  INDETERMINATE: "#8b7ed8",
  QUESTIONED: "#d29922",
  RETRACTED: "#f85149",
  UNAFFECTED: "#8d99a8",
};

const STYLE: cytoscape.StylesheetStyle[] = [
  {
    selector: "node",
    style: {
      label: "data(display)",
      color: "#e6eaef",
      "font-size": 12,
      "font-weight": 600,
      "font-family": "ui-monospace, Menlo, monospace",
      "text-valign": "center",
      "text-halign": "center",
      "text-wrap": "wrap",
      "text-max-width": "132px",
      "border-width": 2,
    },
  },
  {
    // Claims are the things whose status is being decided, so they are the
    // largest and carry the status colour.
    selector: 'node[kind = "claim"]',
    style: {
      shape: "round-rectangle",
      width: 158,
      height: 48,
      "background-color": "#181d23",
      "border-color": (n: cytoscape.NodeSingular) =>
        STATUS_COLOR[(n.data("status") as StatusName) ?? "UNAFFECTED"],
      "border-width": 2.5,
    },
  },
  {
    // Justifications are kept visible rather than collapsed: the threshold
    // lives here, so "2 of 3" is what explains a survival.
    selector: 'node[kind = "justification"]',
    style: {
      shape: "diamond",
      width: 58,
      height: 58,
      "background-color": "#13171c",
      "border-color": "#3d4652",
      "font-size": 11,
      "font-weight": 700,
      color: "#aab4c0",
    },
  },
  {
    selector: 'node[kind = "work"]',
    style: {
      shape: "round-rectangle",
      width: 120,
      height: 34,
      "background-color": "#1b2430",
      "border-color": "#3d4652",
      "font-size": 10,
      color: "#8d99a8",
    },
  },
  {
    selector: 'node[kind = "work"][?retracted]',
    style: {
      "background-color": "#2b1416",
      "border-color": "#f85149",
      "border-width": 3,
      color: "#ff9d96",
      "font-weight": 700,
    },
  },
  {
    // The work under study. Everything in the scene turns on this node, so
    // it is sized and lit to be found without hunting.
    selector: "node.focal",
    style: {
      width: 150,
      height: 46,
      "font-size": 12,
      "border-width": 3.5,
      "overlay-color": "#f85149",
      "overlay-opacity": 0.16,
      "overlay-padding": 9,
    },
  },
  {
    // Briefly marks the nodes whose status just changed, so the eye lands on
    // the consequence rather than scanning for it.
    selector: "node.changed",
    style: {
      "border-width": 5,
      "overlay-color": "#58a6ff",
      "overlay-opacity": 0.3,
      "overlay-padding": 11,
    },
  },
  {
    selector: "edge",
    style: {
      width: 1.6,
      "line-color": "#3d4652",
      "target-arrow-color": "#3d4652",
      "target-arrow-shape": "triangle",
      "arrow-scale": 0.8,
      "curve-style": "bezier",
    },
  },
  {
    // A dead antecedent is the visual answer to "why did this change?".
    selector: "edge[?dead]",
    style: {
      "line-color": "#f85149",
      "target-arrow-color": "#f85149",
      "line-style": "dashed",
      width: 2,
      opacity: 0.85,
    },
  },
  {
    selector: "node:selected",
    style: { "border-color": "#58a6ff", "border-width": 3.5 },
  },
  { selector: ".dimmed", style: { opacity: 0.2 } },
];

interface Props {
  nodes: GraphNode[];
  edges: GraphEdge[];
  onSelect: (id: string | null, kind: string | null) => void;
  highlight: string[];
  focalId?: string;
}

export default function GraphExplorer({
  nodes,
  edges,
  onSelect,
  highlight,
  focalId,
}: Props) {
  const container = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const prevStatus = useRef<Map<string, string>>(new Map());

  // Build once. Later status changes update data in place so the layout does
  // not jump, which is what makes before/after readable as one scene.
  useEffect(() => {
    if (!container.current || cyRef.current) return;
    const withLabels = nodes.map((n) => ({
      data: { ...n.data, display: displayLabel(n.data) },
    }));
    const cy = cytoscape({
      container: container.current,
      elements: { nodes: withLabels, edges },
      style: STYLE,
      layout: {
        name: "dagre",
        rankDir: "BT",
        nodeSep: 42,
        rankSep: 76,
        // Keep content clear of the legend and the headline overlay.
        padding: 30,
        fit: true,
      } as never,
      minZoom: 0.25,
      maxZoom: 2.5,
      wheelSensitivity: 0.25,
    });
    cy.on("tap", "node", (evt) => {
      const n = evt.target as cytoscape.NodeSingular;
      onSelect(n.id(), n.data("kind"));
    });
    cy.on("tap", (evt) => {
      if (evt.target === cy) onSelect(null, null);
    });
    // cy.ready fires before dagre has positioned anything, so fitting there
    // frames an empty graph and the real one ends up clipped.
    cy.one("layoutstop", () => cy.fit(undefined, 40));
    const onResize = () => cy.fit(undefined, 40);
    window.addEventListener("resize", onResize);
    cyRef.current = cy;
    return () => {
      window.removeEventListener("resize", onResize);
      cy.destroy();
      cyRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const changed: string[] = [];
    cy.batch(() => {
      for (const n of nodes) {
        const el = cy.getElementById(n.data.id);
        if (el.empty()) continue;
        const before = prevStatus.current.get(n.data.id);
        const after = n.data.status ?? "";
        if (before !== undefined && before !== after) changed.push(n.data.id);
        prevStatus.current.set(n.data.id, after);
        el.data("status", after);
        el.data("retracted", n.data.retracted);
      }
      for (const e of edges) {
        const el = cy.getElementById(e.data.id);
        if (el.nonempty()) el.data("dead", e.data.dead);
      }
      if (focalId) cy.getElementById(focalId).addClass("focal");
    });

    if (!changed.length) return;
    const marked = cy.collection();
    for (const id of changed) marked.merge(cy.getElementById(id));
    marked.addClass("changed");
    const timer = window.setTimeout(() => marked.removeClass("changed"), 1400);
    return () => window.clearTimeout(timer);
  }, [nodes, edges, focalId]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().removeClass("dimmed");
      if (highlight.length) {
        const keep = cy.collection();
        for (const id of highlight) keep.merge(cy.getElementById(id));
        const neighbourhood = keep.union(keep.connectedEdges());
        cy.elements().difference(neighbourhood).addClass("dimmed");
      }
    });
  }, [highlight]);

  return <div id="cy" ref={container} />;
}
