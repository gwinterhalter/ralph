import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type {
  InboxCard, FleetSnapshot, Finding, EffectRow, Forecast, EventRow, ActionRow, GraphNode, GraphEdge,
  ProjectRow, RunRow, LoopStatus,
} from "./api";
import {
  InboxView, FleetView, RunsView, ImproveView, EffectsView, SpendView, EventsView, ActionsView,
  GraphView, SupervisorControls,
} from "./views";

type Tab = "home" | "fleet" | "runs" | "improve" | "effects" | "spend" | "events" | "graph" | "actions";

const TABS: { id: Tab; label: string }[] = [
  { id: "home", label: "Home" },
  { id: "fleet", label: "Fleet" },
  { id: "runs", label: "Runs" },
  { id: "improve", label: "Improve" },
  { id: "effects", label: "Effects" },
  { id: "spend", label: "Spend" },
  { id: "events", label: "Events" },
  { id: "graph", label: "Graph" },
  { id: "actions", label: "Actions" },
];

function loopLabel(loop: LoopStatus | null): string {
  if (!loop || loop.last_activity === null) return "loop: no activity seen";
  const s = loop.seconds_since ?? 0;
  const ago = s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`;
  return loop.active_guess ? `loop: active (${ago} ago)` : `loop: idle — last activity ${ago} ago`;
}

export default function App() {
  const [tab, setTab] = useState<Tab>("home");
  const [cards, setCards] = useState<InboxCard[]>([]);
  const [fleet, setFleet] = useState<FleetSnapshot | null>(null);
  const [projects, setProjects] = useState<ProjectRow[]>([]);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [runsTotal, setRunsTotal] = useState("0");
  const [loop, setLoop] = useState<LoopStatus | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [effects, setEffects] = useState<EffectRow[]>([]);
  const [byOutcome, setByOutcome] = useState<Record<string, number>>({});
  const [forecast, setForecast] = useState<Forecast | null>(null);
  const [events, setEvents] = useState<EventRow[]>([]);
  const [eventMeta, setEventMeta] = useState<{ total: number; failures: number }>({ total: 0, failures: 0 });
  const [actions, setActions] = useState<ActionRow[]>([]);
  const [graph, setGraph] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] }>({ nodes: [], edges: [] });
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [rowEvents, setRowEvents] = useState<EventRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<number | undefined>(undefined);

  const flash = useCallback((msg: string) => {
    setToast(msg);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4000);
  }, []);

  const loadAll = useCallback(async () => {
    try {
      setError(null);
      const [inbox, fl, pr, rn, lp, learn, eff, fc, ev, acts, gr] = await Promise.all([
        api.inbox(), api.fleet(), api.projects(), api.runs(), api.loopStatus(), api.learnings(),
        api.effects(), api.forecast(), api.events(), api.actions(), api.graph(),
      ]);
      setCards(inbox.cards); setFleet(fl); setProjects(pr.projects);
      setRuns(rn.runs); setRunsTotal(rn.total_cost_usd); setLoop(lp);
      setFindings(learn.findings); setEffects(eff.effects); setByOutcome(eff.by_outcome);
      setForecast(fc); setEvents(ev.events); setEventMeta(ev.metrics);
      setActions(acts.actions); setGraph(gr);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void loadAll();
    const es = new EventSource(api.streamUrl());  // live push for inbox + fleet (no polling)
    es.onmessage = (m) => {
      try {
        const payload = JSON.parse(m.data) as { inbox: { cards: InboxCard[] }; fleet: FleetSnapshot };
        setCards(payload.inbox.cards);
        setFleet(payload.fleet);
      } catch { /* ignore a malformed frame */ }
    };
    es.onerror = () => { /* EventSource auto-reconnects; transient errors are normal */ };
    return () => es.close();
  }, [loadAll]);

  const showProject = useCallback(async (projectId: string) => {
    setTab("fleet");
    try {
      const ev = await api.events(projectId, undefined, 10);
      setRowEvents(ev.events);
      setExpandedId(projectId);
    } catch (e) { setError(String(e)); }
  }, []);

  const toggleRow = useCallback(async (projectId: string) => {
    if (expandedId === projectId) { setExpandedId(null); return; }
    await showProject(projectId);
  }, [expandedId, showProject]);

  const onCardAction = useCallback(
    async (card: InboxCard, action: string) => {
      try {
        if (action === "investigate" || action === "details") { await showProject(card.subject); return; }
        if (card.kind === "gate") { await api.resolveGate(card.subject, action); flash(`Gate answered: ${action}`); }
        else if (card.kind === "learning" && action === "adopt") { await api.promote(card.subject); flash("Adopted — see Improve"); }
        else if (card.kind === "learning" && action === "reject") { await api.reject(card.subject); flash("Rejected"); }
        else if ((card.kind === "stall" || card.kind === "budget") && action === "pause") { await api.pause(card.subject); flash("Pause queued"); }
        await loadAll();
      } catch (e) { setError(String(e)); }
    },
    [loadAll, showProject, flash],
  );

  return (
    <div className="console">
      <header className="topbar">
        <strong>Outer Loop Supervisor</strong>
        <span className="loop-label">{loopLabel(loop)}</span>
        <SupervisorControls
          loop={loop}
          onRunOnce={() => {
            if (window.confirm("Run ONE supervisor cycle now? It dispatches real work (real $)."))
              void api.supervisorRunOnce().then((r) => { flash(`Cycle started (pid ${r.pid})`); return loadAll(); }).catch((e) => setError(String(e)));
          }}
          onStart={() => {
            if (window.confirm("Start the autonomous supervisor loop? It keeps dispatching real work until you Stop it."))
              void api.supervisorStart(30).then((r) => { flash(`Loop started (pid ${r.pid})`); return loadAll(); }).catch((e) => setError(String(e)));
          }}
          onStop={() => void api.supervisorStop().then(() => { flash("Loop stopped"); return loadAll(); }).catch((e) => setError(String(e)))}
        />
        {fleet && <span className="cost">${fleet.total_cumulative_cost_usd}</span>}
      </header>
      <nav className="rail">
        {TABS.map((t) => (
          <button key={t.id} className={tab === t.id ? "active" : ""} onClick={() => setTab(t.id)}>
            {t.label}{t.id === "home" && cards.length > 0 ? ` (${cards.length})` : ""}
          </button>
        ))}
      </nav>
      <main>
        {toast && <p className="toast" role="status">{toast}</p>}
        {error && <p className="error" role="alert">{error}</p>}
        {tab === "home" && (
          <>
            <h2>Needs You</h2>
            <InboxView cards={cards} onAction={onCardAction} />
          </>
        )}
        {tab === "fleet" && (
          <FleetView
            projects={projects}
            snapshot={fleet}
            expandedId={expandedId}
            rowEvents={rowEvents}
            onToggleRow={(p) => void toggleRow(p)}
            onPause={(p) => void api.pause(p).then(() => { flash("Pause queued"); return loadAll(); })}
            onBump={(p) => {
              const cap = window.prompt(`New budget cap (USD) for ${p}?`);
              if (cap) void api.bumpBudget(p, cap).then(() => { flash("Budget bump queued"); return loadAll(); });
            }}
          />
        )}
        {tab === "runs" && <RunsView runs={runs} totalCost={runsTotal} />}
        {tab === "improve" && (
          <ImproveView
            findings={findings}
            effects={effects}
            onPromote={(k) => void api.promote(k).then(() => { flash("Adopted"); return loadAll(); })}
            onReject={(k) => void api.reject(k).then(() => { flash("Rejected"); return loadAll(); })}
            onApply={(k) => void api.apply(k).then(() => { flash("Applied — dispatched to skill"); return loadAll(); }).catch((e) => setError(String(e)))}
            onRevert={(k) => void api.revert(k).then((r) => flash(r.detail)).catch((e) => setError(String(e)))}
          />
        )}
        {tab === "effects" && <EffectsView effects={effects} byOutcome={byOutcome} />}
        {tab === "spend" && (
          <SpendView
            forecast={forecast}
            onProvision={() => {
              if (window.confirm("Provision the ABS Phase 0→1→2 chain as candidate projects?"))
                void api.onramp(true).then(() => { flash("ABS chain provisioned"); return loadAll(); });
            }}
            onPrune={(days) => {
              if (window.confirm(`Delete events older than ${days} days?`))
                void api.prune(days).then(() => { flash("Old events pruned"); return loadAll(); });
            }}
            onQuery={() => void api.query().then(() => { flash("Query-register command queued"); return loadAll(); }).catch((e) => setError(String(e)))}
          />
        )}
        {tab === "events" && <EventsView events={events} total={eventMeta.total} failures={eventMeta.failures} />}
        {tab === "graph" && <GraphView nodes={graph.nodes} edges={graph.edges} />}
        {tab === "actions" && <ActionsView actions={actions} />}
      </main>
    </div>
  );
}
