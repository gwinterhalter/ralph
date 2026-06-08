import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { InboxCard, FleetSnapshot, Finding, EffectRow } from "./api";
import { InboxView, FleetView, ImproveView, EffectsView } from "./views";

type Tab = "home" | "fleet" | "improve" | "effects";

const TABS: { id: Tab; label: string }[] = [
  { id: "home", label: "Home" },
  { id: "fleet", label: "Fleet" },
  { id: "improve", label: "Improve" },
  { id: "effects", label: "Effects" },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("home");
  const [cards, setCards] = useState<InboxCard[]>([]);
  const [fleet, setFleet] = useState<FleetSnapshot | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [effects, setEffects] = useState<EffectRow[]>([]);
  const [byOutcome, setByOutcome] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      const [inbox, fl, learn, eff] = await Promise.all([
        api.inbox(), api.fleet(), api.learnings(), api.effects(),
      ]);
      setCards(inbox.cards);
      setFleet(fl);
      setFindings(learn.findings);
      setEffects(eff.effects);
      setByOutcome(eff.by_outcome);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const onCardAction = useCallback(
    async (card: InboxCard, action: string) => {
      if (card.kind === "learning" && action === "adopt") await api.promote(card.subject);
      else if (card.kind === "learning" && action === "reject") await api.reject(card.subject);
      else if (card.kind === "stall" && action === "pause") await api.pause(card.subject);
      await refresh();
    },
    [refresh],
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
          <button
            key={t.id}
            className={tab === t.id ? "active" : ""}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.id === "home" && cards.length > 0 ? ` (${cards.length})` : ""}
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
            onPause={(p) => void api.pause(p).then(refresh)}
            onBump={(p) => {
              const cap = window.prompt(`New budget cap (USD) for ${p}?`);
              if (cap) void api.bumpBudget(p, cap).then(refresh);
            }}
          />
        )}
        {tab === "improve" && (
          <ImproveView
            findings={findings}
            effects={effects}
            onPromote={(k) => void api.promote(k).then(refresh)}
            onReject={(k) => void api.reject(k).then(refresh)}
            onApply={(k) => void api.apply(k).then(refresh)}
          />
        )}
        {tab === "effects" && <EffectsView effects={effects} byOutcome={byOutcome} />}
      </main>
    </div>
  );
}
