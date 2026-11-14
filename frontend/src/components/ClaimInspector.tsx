import { useEffect, useState } from "react";
import { fetchSupport } from "../api";
import type { StatusName, SupportJustification, Verdict } from "../types";

const TONE: Record<StatusName, string> = {
  SUPPORTED: "ok",
  SUPPORTED_WEAKENED: "warn",
  UNSUPPORTED: "bad",
  UNSUPPORTED_CIRCULAR: "bad",
  INDETERMINATE: "unknown",
  QUESTIONED: "warn",
  RETRACTED: "bad",
  UNAFFECTED: "ok",
};

const LABEL: Record<StatusName, string> = {
  SUPPORTED: "Holds",
  SUPPORTED_WEAKENED: "Holds, weakened",
  UNSUPPORTED: "Loses support",
  UNSUPPORTED_CIRCULAR: "Circular support only",
  INDETERMINATE: "Cannot tell",
  QUESTIONED: "Questioned",
  RETRACTED: "Retracted",
  UNAFFECTED: "Unaffected",
};

const REASON: Record<string, string> = {
  WELL_FOUNDED_SUPPORT: "Grounded in evidence that still stands.",
  LOST_INDEPENDENT_SUPPORT:
    "Still holds, but on fewer independent supports than before.",
  ALL_JUSTIFICATIONS_BELOW_THRESHOLD:
    "Every justification now falls short of its threshold.",
  SUPPORT_ONLY_IN_UNFOUNDED_SET:
    "Supported only by a cycle of beliefs with nothing grounding it.",
  UNINGESTED_ANTECEDENTS:
    "Rests partly on something not yet assessed, so no verdict is claimed.",
  RETRACTED_PREMISE: "This work was retracted.",
  PROCEDURAL_RETRACTION_UPSTREAM:
    "An upstream retraction was procedural, so the finding is flagged rather than refuted.",
};

export default function ClaimInspector({ verdict }: { verdict: Verdict | null }) {
  const [support, setSupport] = useState<SupportJustification[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!verdict) {
      setSupport([]);
      return;
    }
    let live = true;
    setError(null);
    fetchSupport(verdict.belief_id)
      .then((r) => live && setSupport(r.justifications))
      .catch(() => live && setError("No justifications recorded for this node."));
    return () => {
      live = false;
    };
  }, [verdict]);

  if (!verdict) {
    return (
      <div className="empty">
        Select a claim in the graph to see what holds it up.
        <br />
        <br />
        Diamonds are justifications. The number on one is its threshold, so
        <b> 2 of 3</b> means any two of its three antecedents suffice. That is
        why some claims survive a retraction and others do not.
      </div>
    );
  }

  const tone = TONE[verdict.status];
  return (
    <>
      <h3>{verdict.belief_id}</h3>
      <div className="sub">{verdict.reason_code}</div>

      <div style={{ marginBottom: 14 }}>
        <span className={`badge ${tone}`}>{LABEL[verdict.status]}</span>
      </div>

      <div className="claim-text">{REASON[verdict.reason_code] ?? ""}</div>

      <div className="section-label">
        Support: {verdict.live_support} of {verdict.prior_support} before the change
      </div>

      {error && <div className="empty">{error}</div>}

      {support.map((j) => {
        const met = j.n_in >= j.threshold;
        return (
          <div className="just" key={j.justification_id}>
            <div className="top">
              <span className="rule">{j.rule}</span>
              <span className={`thr ${met ? "met" : "unmet"}`}>
                {j.n_in}/{j.antecedents.length} live, needs {j.threshold}
              </span>
            </div>
            {j.antecedents.map((a) => {
              const state = a.retracted || a.label === "OUT"
                ? "out"
                : a.label === "UNKNOWN"
                  ? "unknown"
                  : "in";
              return (
                <div className="ant" key={a.belief_id}>
                  <span className={`mark ${state}`}>
                    {state === "out" ? "x" : state === "unknown" ? "?" : "+"}
                  </span>
                  <div className="body">
                    <div className="id">{a.belief_id}</div>
                    <div className="ttl">{a.title ?? ""}</div>
                  </div>
                </div>
              );
            })}
            <div style={{ marginTop: 8 }}>
              <span className={`tier ${j.tier}`}>{j.tier}</span>
            </div>
          </div>
        );
      })}

      {verdict.witness_chain.length > 0 && (
        <>
          <div className="section-label">Derivation</div>
          <div className="chain">
            {verdict.witness_chain.map((step, i) => (
              <span key={step}>
                {i > 0 && <span className="arrow"> -&gt; </span>}
                {step}
              </span>
            ))}
          </div>
        </>
      )}

      {support.some((j) => j.tier === "synthetic") && (
        <div className="note">
          The logical structure shown here is synthetic and is not attributed to
          any real document. The works, citations and retraction dates are real.
        </div>
      )}
    </>
  );
}
