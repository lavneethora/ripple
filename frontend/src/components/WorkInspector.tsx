import { useEffect, useState } from "react";
import type { WorkDetail } from "../types";

interface Props {
  workId: string;
  /** Changes whenever a propagation runs, so the panel refetches. */
  epoch: number;
}

export default function WorkInspector({ workId, epoch }: Props) {
  const [work, setWork] = useState<WorkDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setWork(null);
    setError(null);
    fetch(`/api/works/${encodeURIComponent(workId)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d) => live && setWork(d))
      .catch(() => live && setError("Could not load this work."));
    return () => {
      live = false;
    };
    // epoch is a dependency because the retraction toggle mutates the work,
    // and without it the panel kept showing the previous state.
  }, [workId, epoch]);

  if (error) return <div className="empty">{error}</div>;
  if (!work) return <div className="empty">Loading…</div>;

  // The badge reports the durable fact, which is whether a retraction event
  // exists in the record. It deliberately does not read is_retracted: that
  // flag is simulation state the "Study stands" toggle flips, so using it
  // made the badge say "Not retracted" directly above a retraction date.
  const retractedInRecord = work.retracted_on !== null;
  const badge = retractedInRecord
    ? work.reason_class === "SUBSTANTIVE"
      ? { tone: "bad", text: "Retracted" }
      : {
          tone: "warn",
          text: `Retracted, ${(work.reason_class ?? "unknown").toLowerCase()}`,
        }
    : work.flag_disputed
      ? { tone: "unknown", text: "Retraction flag disputed" }
      : { tone: "ok", text: "Not retracted" };

  // Make the simulated state explicit rather than letting it contradict the
  // record elsewhere in the panel.
  const simulatedAsStanding = retractedInRecord && !work.is_retracted;

  return (
    <>
      <h3>{work.short}</h3>
      <div className="sub">{work.work_id}</div>

      <div style={{ marginBottom: 14 }}>
        <span className={`badge ${badge.tone}`}>{badge.text}</span>
        {simulatedAsStanding && (
          <span className="badge unknown" style={{ marginLeft: 6 }}>
            treated as standing
          </span>
        )}
      </div>

      <div className="claim-text">{work.title}</div>

      <div className="kv">
        {work.authors.length > 0 && (
          <div>
            <span>Authors</span>
            <b>
              {work.authors.slice(0, 3).join(", ")}
              {work.authors.length > 3 ? " et al." : ""}
            </b>
          </div>
        )}
        {work.venue && (
          <div>
            <span>Venue</span>
            <b>{work.venue}</b>
          </div>
        )}
        <div>
          <span>Published</span>
          <b>{work.published ?? "unknown"}</b>
        </div>
        <div>
          <span>Cited</span>
          <b>{work.cited_by_count.toLocaleString()} times</b>
        </div>
        {work.retracted_on && (
          <div>
            <span>Retracted</span>
            <b>{work.retracted_on}</b>
          </div>
        )}
      </div>

      {work.doi && (
        <a className="doi" href={`https://doi.org/${work.doi}`} target="_blank" rel="noreferrer">
          {work.doi}
        </a>
      )}

      {work.reason_text && (
        <>
          <div className="section-label">Why it was retracted</div>
          <div className="reason">{work.reason_text}</div>
        </>
      )}

      {work.flag_disputed && !work.is_retracted && (
        <div className="note">
          OpenAlex flags this work as retracted. Crossref holds no retraction
          record for it, so RIPPLE treats it as not retracted.
        </div>
      )}

      {work.cites_retracted_work && work.cites_retracted_work.lag !== null && (
        <div className="note">
          Cites the retracted study <b>{work.cites_retracted_work.lag} days</b>{" "}
          after its retraction ({work.cites_retracted_work.awareness}).
        </div>
      )}

      {work.supports.length > 0 && (
        <>
          <div className="section-label">Conclusions resting on it</div>
          {work.supports.map((s) => (
            <div className="ant" key={s.justification}>
              <span className="mark in">-</span>
              <div className="body">
                <div className="ttl">{s.short}</div>
                <div className="id">
                  {s.status ?? "not evaluated"} · needs {s.threshold}
                </div>
              </div>
            </div>
          ))}
        </>
      )}
    </>
  );
}
