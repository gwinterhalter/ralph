// Presentational views — pure functions of props (no fetching), so they unit-test cleanly.
import type {
  InboxCard, FleetSnapshot, Finding, EffectRow, Forecast, EventRow, ActionRow, GraphNode, GraphEdge,
  ProjectRow, RunRow, LoopStatus, Throttling,
} from "./api";

export function SupervisorControls(props: {
  loop: LoopStatus | null;
  onRunOnce: () => void;
  onStart: () => void;
  onStop: () => void;
}) {
  const running = !!props.loop?.managed_running;
  return (
    <span className="sup-controls">
      <span className={running ? "live" : "idle"}>{running ? "● loop running" : "○ loop stopped"}</span>
      {running
        ? <button onClick={props.onStop}>Stop</button>
        : <button onClick={props.onStart}>Start loop</button>}
      <button onClick={props.onRunOnce}>Run once</button>
    </span>
  );
}

const KIND_ICON: Record<string, string> = {
  budget: "🔴",
  gate: "🔴",
  stall: "🟠",
  failed: "🟥",
  approval: "🟦",
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

const ACTIVE_STATES = new Set(["running", "candidate", "admitted", "paused_gate"]);

export function FleetView(props: {
  projects: ProjectRow[];
  snapshot: FleetSnapshot | null;        // for the live running/stalled rollup only
  expandedId: string | null;
  rowEvents: EventRow[];
  onToggleRow: (projectId: string) => void;
  onPause: (projectId: string) => void;
  onBump: (projectId: string) => void;
}) {
  const s = props.snapshot;
  if (props.projects.length === 0) return <p>No projects yet. Provision work from the Spend tab.</p>;
  return (
    <div>
      <table className="fleet" aria-label="Fleet">
        <thead>
          <tr><th></th><th>Project</th><th>Lifecycle</th><th>Cost</th><th>Runs</th><th>Attn</th><th>Depends on</th><th></th></tr>
        </thead>
        <tbody>
          {props.projects.map((r) => (
            <FleetRow
              key={r.project_id}
              row={r}
              expanded={props.expandedId === r.project_id}
              events={props.expandedId === r.project_id ? props.rowEvents : []}
              onToggle={() => props.onToggleRow(r.project_id)}
              onPause={() => props.onPause(r.project_id)}
              onBump={() => props.onBump(r.project_id)}
            />
          ))}
        </tbody>
      </table>
      {s && (
        <p className="rollup">
          Live: running {s.running_count}/{s.concurrency_ceiling} · headroom {s.headroom} ·
          stalled {s.stalled_count} · fleet cost ${s.total_cumulative_cost_usd} (info)
        </p>
      )}
    </div>
  );
}

function FleetRow(props: {
  row: ProjectRow;
  expanded: boolean;
  events: EventRow[];
  onToggle: () => void;
  onPause: () => void;
  onBump: () => void;
}) {
  const r = props.row;
  const active = ACTIVE_STATES.has(r.lifecycle_state);
  return (
    <>
      <tr className={`lc-${r.lifecycle_state}`}>
        <td>
          <button aria-label={`expand ${r.project_id}`} className="expander" onClick={props.onToggle}>
            {props.expanded ? "▾" : "▸"}
          </button>
        </td>
        <td>{r.display_name}</td>
        <td><span className={`badge lc-${r.lifecycle_state}`}>{r.lifecycle_state}</span></td>
        <td>${r.cost_usd}</td><td>{r.runs}</td><td>{r.attention_debt}</td>
        <td>{r.depends_on.length ? r.depends_on.join(", ") : "—"}</td>
        <td>
          {active && <button onClick={props.onPause}>Pause</button>}
          {active && <button onClick={props.onBump}>$</button>}
        </td>
      </tr>
      {props.expanded && (
        <tr className="detail-row">
          <td></td>
          <td colSpan={7}>
            <div className="row-detail" aria-label={`detail ${r.project_id}`}>
              <strong>Recent events</strong>
              {props.events.length === 0 ? <p>No recent events.</p> : (
                <ul className="events">
                  {props.events.map((e, i) => (
                    <li key={`${e.ts_utc}:${i}`}>
                      <span className="e-ts">{e.ts_utc}</span> {e.role}/{e.event_type}
                      {e.subject_id ? ` · ${e.subject_id}` : ""}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export function RunsView(props: { runs: RunRow[]; totalCost: string }) {
  return (
    <div>
      <h2>Run history</h2>
      {props.runs.length === 0 ? <p>No completed runs yet.</p> : (
        <>
          <p className="rollup">{props.runs.length} run(s) · total ${props.totalCost}</p>
          <table className="fleet" aria-label="Runs">
            <thead><tr><th>Project</th><th>Status</th><th>Cost</th><th>Spawned</th><th>Terminated</th></tr></thead>
            <tbody>
              {props.runs.map((r) => (
                <tr key={r.run_id} className={`lc-${r.status}`}>
                  <td>{r.project_id}</td>
                  <td><span className={`badge lc-${r.status}`}>{r.status}</span></td>
                  <td>${r.cost_usd}</td>
                  <td className="e-ts">{r.spawned_at ?? "—"}</td>
                  <td className="e-ts">{r.terminated_at ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

export function GraphView(props: { nodes: GraphNode[]; edges: GraphEdge[] }) {
  // Layered dependency view: depth = longest depends_on chain to a root. Each node lists what it
  // depends on (the Item 1 cross-initiative gate edges). Pure + deterministic.
  const depsOf = new Map<string, string[]>();
  props.nodes.forEach((n) => depsOf.set(n.id, []));
  props.edges.forEach((e) => depsOf.get(e.from)?.push(e.to));
  const depth = (id: string, seen = new Set<string>()): number => {
    if (seen.has(id)) return 0;
    seen.add(id);
    const ds = depsOf.get(id) ?? [];
    return ds.length === 0 ? 0 : 1 + Math.max(...ds.map((d) => depth(d, seen)));
  };
  const levels: GraphNode[][] = [];
  props.nodes.forEach((n) => {
    const lvl = depth(n.id);
    (levels[lvl] ??= []).push(n);
  });
  return (
    <div>
      <h2>Dependency graph</h2>
      {props.nodes.length === 0 ? <p>No projects.</p> : (
        <div className="graph" aria-label="Dependency graph">
          {levels.map((nodesAtLevel, lvl) => (
            <div key={lvl} className="graph-level">
              <div className="graph-level-label">level {lvl}</div>
              {nodesAtLevel.map((n) => (
                <div key={n.id} className={`graph-node lc-${n.lifecycle_state}`} data-node={n.id}>
                  <div className="gn-id">{n.id}</div>
                  <div className="gn-state">{n.lifecycle_state}</div>
                  {(depsOf.get(n.id) ?? []).length > 0 && (
                    <div className="gn-deps">→ {(depsOf.get(n.id) ?? []).join(", ")}</div>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
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
  onRevert: (key: string) => void;
}) {
  const effectByKey = new Map(props.effects.map((e) => [e.finding_key, e]));
  if (props.findings.length === 0) {
    return (
      <p className="empty-help">
        No learnings captured yet. The supervisor's Learn step writes Run-Auditor findings here after
        it audits completed runs (recurring gates, over-verification, chronic corrections). They then
        flow Proposed → Accepted → Applied, and the Effects tab measures whether each one helped.
      </p>
    );
  }
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
                    {col === "applied" && (
                      <button className="danger" onClick={() => props.onRevert(f.finding_key)}>Roll back</button>
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
  if (props.effects.length === 0) {
    return (
      <p className="empty-help">
        No effects measured yet. Once a learning is <em>applied</em> (Improve tab), the supervisor
        compares the subject's runs before vs after adoption and reports here whether it
        helped (confirmed), did nothing (no_effect), or made things worse (regressed).
      </p>
    );
  }
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
  throttling?: Throttling | null;
  onProvision: () => void;
  onPrune: (days: number) => void;
  onQuery: () => void;
}) {
  const f = props.forecast;
  const th = props.throttling;
  return (
    <div>
      <h2>Spend</h2>
      {th && (th.count > 0 ? (
        <div className="throttle-warn" aria-label="Throttling">
          <p className="rollup">⚠ Throttled {th.count}× — rate / usage limits hit</p>
          <ul className="events">
            {th.recent.slice(0, 5).map((t, i) => (
              <li key={`${t.ts_utc}:${i}`}>
                <span className="e-ts">{t.ts_utc}</span> {t.project_id} · {t.role}
                {t.reset_hint ? ` · ${t.reset_hint}` : ""}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="rollup" aria-label="Throttling">No throttling recorded ✓</p>
      ))}
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
        <button onClick={props.onQuery}>Query register state</button>
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
