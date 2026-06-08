// Presentational views — pure functions of props (no fetching), so they unit-test cleanly.
import type {
  InboxCard, FleetSnapshot, Finding, EffectRow, Forecast, EventRow, ActionRow,
} from "./api";

const KIND_ICON: Record<string, string> = {
  budget: "🔴",
  gate: "🔴",
  stall: "🟠",
  regressed: "🟣",
  learning: "🟡",
  churn: "⚪",
};

export function InboxView(props: {
  cards: InboxCard[];
  onAction: (card: InboxCard, action: string) => void;
}) {
  if (props.cards.length === 0) {
    return <p className="all-clear" role="status">Nothing needs you. Fleet is healthy. ✓</p>;
  }
  return (
    <ul className="inbox" aria-label="Needs you">
      {props.cards.map((c) => (
        <li key={`${c.kind}:${c.subject}`} className={`card card-${c.kind}`} data-kind={c.kind}>
          <div className="card-title">
            <span className="icon">{KIND_ICON[c.kind] ?? "•"}</span> {c.title}
          </div>
          <div className="card-detail">{c.detail}</div>
          <div className="card-actions">
            {c.actions.map((a) => (
              <button
                key={a}
                className={a === c.recommended ? "recommended" : ""}
                onClick={() => props.onAction(c, a)}
              >
                {a.replace(/_/g, " ")}
              </button>
            ))}
          </div>
        </li>
      ))}
    </ul>
  );
}

export function FleetView(props: {
  snapshot: FleetSnapshot | null;
  onPause: (projectId: string) => void;
  onBump: (projectId: string) => void;
}) {
  const s = props.snapshot;
  if (!s) return <p>Loading fleet…</p>;
  return (
    <div>
      <table className="fleet" aria-label="Fleet">
        <thead>
          <tr><th>Project</th><th>Lifecycle</th><th>Run</th><th>Attn</th><th>Open</th><th>Cost</th><th>♥</th><th></th></tr>
        </thead>
        <tbody>
          {s.rows.map((r) => (
            <tr key={r.project_id} data-stalled={r.heartbeat_state === "STALLED"}>
              <td>{r.display_name}</td>
              <td>{r.lifecycle_state}</td>
              <td>{r.active_run_status}</td>
              <td>{r.attention_debt}</td>
              <td>{r.open_work_count}</td>
              <td>${r.cumulative_cost_usd}</td>
              <td>{r.heartbeat_state}</td>
              <td>
                <button onClick={() => props.onPause(r.project_id)}>Pause</button>
                <button onClick={() => props.onBump(r.project_id)}>$</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="rollup">
        Running {s.running_count}/{s.concurrency_ceiling} · headroom {s.headroom} · stalled{" "}
        {s.stalled_count} · total ${s.total_cumulative_cost_usd} (info)
      </p>
    </div>
  );
}

const COLUMNS = ["proposed", "accepted", "applied", "rejected"] as const;

export function ImproveView(props: {
  findings: Finding[];
  effects: EffectRow[];
  onPromote: (key: string) => void;
  onReject: (key: string) => void;
  onApply: (key: string) => void;
}) {
  const effectByKey = new Map(props.effects.map((e) => [e.finding_key, e]));
  return (
    <div className="kanban">
      {COLUMNS.map((col) => {
        const cards = props.findings.filter((f) => (f.status || "proposed") === col);
        return (
          <section key={col} className="kanban-col" aria-label={col}>
            <h3>{col} ({cards.length})</h3>
            {cards.map((f) => {
              const eff = col === "applied" ? effectByKey.get(f.finding_key) : undefined;
              return (
                <div key={f.finding_key} className="kanban-card">
                  <div className="k-kind">{f.kind} · {f.subject}</div>
                  <div className="k-rec">{f.recommendation}</div>
                  {f.authoring_skill && <div className="k-skill">→ {f.authoring_skill}</div>}
                  {eff && <div className={`k-effect effect-${eff.outcome}`}>effect: {eff.outcome}</div>}
                  <div className="k-actions">
                    {col === "proposed" && (
                      <>
                        <button onClick={() => props.onPromote(f.finding_key)}>Adopt</button>
                        <button onClick={() => props.onReject(f.finding_key)}>Reject</button>
                      </>
                    )}
                    {col === "accepted" && (
                      <button onClick={() => props.onApply(f.finding_key)}>Apply</button>
                    )}
                  </div>
                </div>
              );
            })}
          </section>
        );
      })}
    </div>
  );
}

export function EffectsView(props: { effects: EffectRow[]; byOutcome: Record<string, number> }) {
  const order = ["regressed", "no_effect", "pending", "confirmed"];
  const roll = order.filter((o) => props.byOutcome[o]).map((o) => `${props.byOutcome[o]} ${o}`).join(", ");
  return (
    <div>
      <p className="rollup">{roll || "none measured yet"}</p>
      <ul className="effects">
        {props.effects.map((e) => (
          <li key={e.finding_key} className={`effect-${e.outcome}`}>
            <strong>[{e.outcome}]</strong> {e.finding_key} · {fmt(e.before_metric)} → {fmt(e.after_metric)}{" "}
            over {e.post_adoption_runs} post-run(s)
            {e.detail && <div className="e-detail">{e.detail}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function fmt(v: number | null): string {
  return typeof v === "number" ? v.toFixed(3) : "?";
}

export function SpendView(props: {
  forecast: Forecast | null;
  onProvision: () => void;
  onPrune: (days: number) => void;
}) {
  const f = props.forecast;
  return (
    <div>
      <h2>Spend</h2>
      {!f ? <p>Loading forecast…</p> : (
        <>
          <p className="rollup">
            Fleet spent ${f.fleet_spent_usd} · projected remaining ${f.fleet_projected_remaining_usd} ·
            projected total ${f.fleet_projected_total_usd}
          </p>
          <table className="fleet" aria-label="Forecast">
            <thead>
              <tr><th>Project</th><th>Basis</th><th>Open</th><th>Spent</th><th>Proj. remaining</th><th>Conf</th></tr>
            </thead>
            <tbody>
              {f.projects.map((p) => (
                <tr key={p.project_id}>
                  <td>{p.project_id}</td><td>{p.basis}</td><td>{p.open_work_count}</td>
                  <td>${p.total_spent_usd}</td><td>${p.projected_remaining_usd}</td>
                  <td>{(p.confidence * 100).toFixed(0)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <div className="admin-actions">
        <button onClick={props.onProvision}>Provision ABS chain…</button>
        <button onClick={() => props.onPrune(30)}>Prune events &gt; 30d</button>
      </div>
    </div>
  );
}

export function EventsView(props: { events: EventRow[]; total: number; failures: number }) {
  return (
    <div>
      <h2>Events</h2>
      <p className="rollup">{props.total} event(s) · {props.failures} failures/halts</p>
      <ul className="events" aria-label="Events">
        {props.events.map((e, i) => (
          <li key={`${e.ts_utc}:${i}`}>
            <span className="e-ts">{e.ts_utc}</span> {e.project_id} · {e.role}/{e.event_type}
            {e.subject_id ? ` · ${e.subject_id}` : ""}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ActionsView(props: { actions: ActionRow[] }) {
  return (
    <div>
      <h2>Operator actions</h2>
      {props.actions.length === 0 ? <p>No actions recorded yet.</p> : (
        <ul className="events" aria-label="Operator actions">
          {props.actions.map((a, i) => (
            <li key={`${a.ts}:${i}`}>
              <span className="e-ts">{a.ts}</span> <strong>{a.action}</strong> {a.target}
              {a.detail ? ` (${a.detail})` : ""} — {a.by}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
