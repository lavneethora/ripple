import { useEffect, useMemo, useState } from "react";
import GraphExplorer from "./components/GraphExplorer";
import ClaimInspector from "./components/ClaimInspector";
import { fetchScenario, propagate } from "./api";
import type { PropagateResponse, Scenario, StatusName } from "./types";

const SUMMARY_ORDER: { key: StatusName; label: string; tone: string }[] = [
  { key: "SUPPORTED", label: "Holds", tone: "ok" },
  { key: "SUPPORTED_WEAKENED", label: "Holds, weakened", tone: "warn" },
  { key: "INDETERMINATE", label: "Cannot tell", tone: "unknown" },
  { key: "UNSUPPORTED_CIRCULAR", label: "Circular only", tone: "bad" },
  { key: "UNSUPPORTED", label: "Loses support", tone: "bad" },
];

export default function App() {
  const [scenario, setScenario] = useState<Scenario | null>(null);
  const [result, setResult] = useState<PropagateResponse | null>(null);
  const [retracted, setRetracted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchScenario().then(setScenario).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    setBusy(true);
    propagate(retracted)
      .then(setResult)
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  }, [retracted]);

  const verdict = useMemo(
    () => result?.verdicts.find((v) => v.belief_id === selected) ?? null,
    [result, selected],
  );

  const highlight = useMemo(
    () => (verdict ? [verdict.belief_id, ...verdict.witness_chain] : []),
    [verdict],
  );

  const unambiguous = scenario?.exposure.find((e) => e.awareness === "unambiguous");
  const lost = result ? (result.summary.UNSUPPORTED ?? 0) : 0;
  const circular = result ? (result.summary.UNSUPPORTED_CIRCULAR ?? 0) : 0;
  const held = result
    ? (result.summary.SUPPORTED ?? 0) + (result.summary.SUPPORTED_WEAKENED ?? 0)
    : 0;

  if (error) return <div className="err">{error}</div>;

  return (
    <div className="app">
      <header className="header">
        <div className="brand">RIP<span>P</span>LE</div>
        <div className="tagline">
          Which of our conclusions still stand if this paper was retracted?
        </div>
        <div className="spacer" />
        <div className="toggle">
          <button
            className={!retracted ? "active" : ""}
            disabled={busy}
            onClick={() => setRetracted(false)}
          >
            Study stands
          </button>
          <button
            className={retracted ? "active danger" : ""}
            disabled={busy}
            onClick={() => setRetracted(true)}
          >
            Study retracted
          </button>
        </div>
      </header>

      {scenario && (
        <div className="scenario">
          <span className="focal">{scenario.focal.title.slice(0, 64)}</span>
          <span className="meta">
            retracted <b>{scenario.focal.retracted_on}</b>
          </span>
          <span className="meta">
            cited <b>{scenario.focal.cited_by_count.toLocaleString()}</b> times
          </span>
          {unambiguous && (
            <span className="meta">
              <b>{unambiguous.n}</b> unambiguous post-retraction citations in store,
              up to <b>{unambiguous.max_lag}</b> days after
            </span>
          )}
          <span className="meta">
            store <b>{scenario.store.nodes.toLocaleString()}</b> nodes
          </span>
          <a href={`https://doi.org/${scenario.focal.doi}`} target="_blank" rel="noreferrer">
            doi
          </a>
        </div>
      )}

      <div className="main">
        <div className="graph-wrap">
          {result && (
            <GraphExplorer
              nodes={result.graph.nodes}
              edges={result.graph.edges}
              onSelect={(id) => setSelected(id)}
              highlight={highlight}
            />
          )}

        </div>

        <aside className="side">
        <div className="headline">
          <h4>Exposure is not loss of support</h4>
          <div className="big">
            <b>{scenario?.focal.cited_by_count.toLocaleString() ?? "-"}</b> works
            cite this study.
            {unambiguous && (
              <>
                {" "}
                <b>{unambiguous.n}</b> of those in this store cite it after the
                retraction.
              </>
            )}
            <div className="split">
              Of the conclusions modelled here, <b>{lost + circular}</b> actually
              lose support and <b>{held}</b> survive on independent evidence.
            </div>
          </div>
        </div>

        <div className="section-label">
            {retracted ? "After the retraction" : "While the study stands"}
          </div>
          <div className="summary">
            {SUMMARY_ORDER.filter((s) => result?.summary[s.key]).map((s) => (
              <div className={`bar ${s.tone}`} key={s.key}>
                <span>{s.label}</span>
                <b>{result?.summary[s.key]}</b>
              </div>
            ))}
          </div>
          <ClaimInspector verdict={verdict} />
          <div className="legend">
            <div className="rows">
            <h4>Status</h4>
            {SUMMARY_ORDER.map((s) => (
              <div className="row" key={s.key}>
                <span
                  className="dot"
                  style={{
                    background:
                      s.tone === "ok"
                        ? "var(--ok)"
                        : s.tone === "warn"
                          ? "var(--warn)"
                          : s.tone === "unknown"
                            ? "var(--unknown)"
                            : "var(--bad)",
                  }}
                />
                {s.label}
              </div>
            ))}
          </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
