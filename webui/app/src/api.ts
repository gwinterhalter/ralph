// Typed client for the control-panel API (webui.server.app). All reads are GET, actions POST.

export interface InboxCard {
  kind: "budget" | "gate" | "stall" | "regressed" | "learning" | "churn";
  urgency: number;
  title: string;
  subject: string;
  detail: string;
  actions: string[];
  recommended: string | null;
}

export interface FleetRow {
  project_id: string;
  display_name: string;
  lifecycle_state: string;
  active_run_status: string;
  attention_debt: number;
  open_work_count: number;
  cumulative_cost_usd: string;
  heartbeat_state: string;
}

export interface FleetSnapshot {
  as_of: string;
  rows: FleetRow[];
  running_count: number;
  concurrency_ceiling: number;
  headroom: number;
  stalled_count: number;
  total_cumulative_cost_usd: string;
}

export interface Finding {
  finding_key: string;
  kind: string;
  subject: string;
  status: string;
  recommendation?: string;
  routes_to?: string;
  authoring_skill?: string;
  runs_audited?: number;
}

export interface EffectRow {
  finding_key: string;
  outcome: string;
  before_metric: number | null;
  after_metric: number | null;
  post_adoption_runs: number;
  applied_at?: string;
  detail?: string;
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return (await res.json()) as T;
}

async function post<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return (await res.json()) as T;
}

export const api = {
  inbox: () => getJSON<{ cards: InboxCard[]; count: number }>("/api/inbox"),
  fleet: () => getJSON<FleetSnapshot>("/api/fleet"),
  learnings: (status?: string) =>
    getJSON<{ findings: Finding[]; by_status: Record<string, number> }>(
      "/api/learnings" + (status ? `?status=${encodeURIComponent(status)}` : ""),
    ),
  effects: () => getJSON<{ effects: EffectRow[]; by_outcome: Record<string, number> }>("/api/effects"),
  pause: (projectId: string, by = "operator") =>
    post(`/api/projects/${encodeURIComponent(projectId)}/pause`, { by }),
  bumpBudget: (projectId: string, newCapUsd: string, by = "operator") =>
    post(`/api/projects/${encodeURIComponent(projectId)}/budget`, { new_cap_usd: newCapUsd, by }),
  promote: (key: string, by = "operator") =>
    post(`/api/findings/${encodeURIComponent(key)}/promote`, { by }),
  reject: (key: string, by = "operator") =>
    post(`/api/findings/${encodeURIComponent(key)}/reject`, { by }),
  apply: (key: string, by = "operator") =>
    post(`/api/findings/${encodeURIComponent(key)}/apply`, { by }),
};
