export type StatusName =
  | "SUPPORTED"
  | "SUPPORTED_WEAKENED"
  | "UNSUPPORTED"
  | "UNSUPPORTED_CIRCULAR"
  | "INDETERMINATE"
  | "QUESTIONED"
  | "RETRACTED"
  | "UNAFFECTED";

export interface Verdict {
  belief_id: string;
  status: StatusName;
  reason_code: string;
  witness: string | null;
  witness_chain: string[];
  killers: string[];
  missing: string[];
  live_support: number;
  prior_support: number;
  holds: boolean;
}

export interface GraphNode {
  data: {
    id: string;
    kind: "claim" | "justification" | "work";
    label: string;
    text?: string;
    doi?: string | null;
    tier?: string;
    status?: StatusName;
    retracted?: boolean;
    threshold?: number;
    arity?: number;
    rule?: string;
    confidence?: number;
  };
}

export interface GraphEdge {
  data: {
    id: string;
    source: string;
    target: string;
    kind: "antecedent" | "supports";
    dead?: boolean;
  };
}

export interface PropagateResponse {
  retracted: boolean;
  epoch: number;
  summary: Record<string, number>;
  verdicts: Verdict[];
  n_beliefs: number;
  n_justifications: number;
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
}

export interface Scenario {
  focal: {
    id: string;
    title: string;
    doi: string;
    retracted_on: string;
    source: string;
    cited_by_count: number;
    reason_class: string;
  };
  exposure: { awareness: string; n: number; min_lag: number; max_lag: number }[];
  late_citations: {
    id: string;
    title: string;
    type: string;
    published: string;
    lag: number;
  }[];
  guidance: {
    work_id: string;
    also_retracted: { id: string; title: string; doi: string }[];
  };
  store: { nodes: number; relationships: number };
}

export interface SupportAntecedent {
  belief_id: string;
  kind: string;
  title: string | null;
  label: string | null;
  retracted: boolean;
}

export interface SupportJustification {
  justification_id: string;
  rule: string;
  threshold: number;
  confidence: number;
  tier: string;
  antecedents: SupportAntecedent[];
  n_in: number;
  n_out: number;
  n_retracted: number;
}
