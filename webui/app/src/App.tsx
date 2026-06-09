import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type {
  InboxCard, FleetSnapshot, Finding, EffectRow, Forecast, EventRow, ActionRow, GraphNode, GraphEdge,
} from "./api";
import {
  InboxView, FleetView, ImproveView, EffectsView, SpendView, EventsView, ActionsView, GraphView,
} from "./views";

type Tab = "home" | "fleet" | "improve" | "effects" | "spend" | "events" | "graph" | "actions";

const TABS: { id: Tab; label: string }[] = [
  { id: "home", label: "Home" },
  { id: "fleet", label: "Fleet" },
  { id: "improve", label: "Improve" },
  { id: "effects", label: "Effects" },
  { id: "spend", label: "Spend" },
  { id: "events", label: "Events" },
  { id: "graph", label: "Graph" },
  { id: "actions", label: "Actions" },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("home");
  const [cards, setCards] = useState<InboxCard[]>([]);
  const [fleet, setFleet] = useState<FleetSnapshot | null>(null);
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

  // The slow-changing data (everything except the live inbox/fleet, which arrive via SSE).
  const loadAll = useCallback(async () => {
    try {
      setError(null);
      const [inbox, fl, learn, eff, fc, ev, acts, gr] = await Promise.all([
        api.inbox(), api.fleet(), api.learnings(), api.effects(), api.forecast(), api.events(),
        api.actions(), api.graph(),
      ]);
      setCards(inbox.cards); setFleet(fl);
      setFindings(learn.findings); setEffects(eff.effects); setByOutcome(eff.by_outcome);
      setForecast(fc); setEvents(ev.events); setEventMeta(ev.metrics);
      setActions(acts.actions); setGraph(gr);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void loadAll();
    // Live push for the inbox + fleet (replaces client polling).
    const es = new EventSource(api.streamUrl());
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

  const toggleRow = useCallback(async (projectId: string) => {
    if (expandedId === projectId) { setExpandedId(null); return; }
    try {
      const ev = await api.events(projectId, undefined, 10);
      setRowEvents(ev.events);
      setExpandedId(projectId);
    } catch (e) { setError(String(e)); }
  }, [expandedId]);

  const onCardAction = useCallback(
    async (card: InboxCard, action: string) => {
      if (card.kind === "gate" && action !== "details") await api.resolveGate(card.subject, action);
      else if (card.kind === "learning" && action === "adopt") await api.promote(card.subject);
      else if (card.kind === "learning" && action === "reject") await api.reject(card.subject);
      else if ((card.kind === "stall" || card.kind === "budget") && action === "pause") await api.pause(card.subject);
      await loadAll();
    },
    [loadAll],
  );

  return (
    <div className="console">
      <header className="topbar">
        <strong>Outer Loop Supervisor</strong>
        <span className="live">● live</span>
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
        {error && <p className="error" role="alert">{error}</p>}
        {tab === "home" && (
          <>
            <h2>Needs You</h2>
            <InboxView cards={cards} onAction={onCardAction} />
          </>
        )}
        {tab === "fleet" && (
          <FleetView
            snapshot={fleet}
            expandedId={expandedId}
            rowEvents={rowEvents}
            onToggleRow={(p) => void toggleRow(p)}
            onPause={(p) => void api.pause(p).then(loadAll)}
            onBump={(p) => {
              const cap = window.prompt(`New budget cap (USD) for ${p}?`);
              if (cap) void api.bumpBudget(p, cap).then(loadAll);
            }}
          />
        )}
        {tab === "improve" && (
          <ImproveView
            findings={findings}
            effects={effects}
            onPromote={(k) => void api.promote(k).then(loadAll)}
            onReject={(k) => void api.reject(k).then(loadAll)}
            onApply={(k) => void api.apply(k).then(loadAll).catch((e) => setError(String(e)))}
          />
        )}
        {tab === "effects" && <EffectsView effects={effects} byOutcome={byOutcome} />}
        {tab === "spend" && (
          <SpendView
            forecast={forecast}
            onProvision={() => {
              if (window.confirm("Provision the ABS Phase 0→1→2 chain as candidate projects?"))
                void api.onramp(true).then(loadAll);
            }}
            onPrune={(days) => {
              if (window.confirm(`Delete events older than ${days} days?`))
                void api.prune(days).then(loadAll);
            }}
          />
        )}
        {tab === "events" && <EventsView events={events} total={eventMeta.total} failures={eventMeta.failures} />}
        {tab === "graph" && <GraphView nodes={graph.nodes} edges={graph.edges} />}
        {tab === "actions" && <ActionsView actions={actions} />}
      </main>
    </div>
  );
}
