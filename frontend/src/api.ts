import type { PropagateResponse, Scenario, SupportJustification } from "./types";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} returned ${res.status}`);
  return res.json() as Promise<T>;
}

export const fetchScenario = () => get<Scenario>("/api/scenario");

export async function propagate(retracted: boolean): Promise<PropagateResponse> {
  const res = await fetch("/api/propagate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ retracted }),
  });
  if (!res.ok) throw new Error(`propagate returned ${res.status}`);
  return res.json();
}

export const fetchSupport = (claimId: string) =>
  get<{ claim_id: string; justifications: SupportJustification[] }>(
    `/api/claims/${encodeURIComponent(claimId)}/support`,
  );
