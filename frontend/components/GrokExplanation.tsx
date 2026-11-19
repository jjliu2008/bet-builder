"use client";
import { useEffect, useState } from "react";
import { explainRoute, grokAvailable } from "@/lib/api";

const TOPICS = [
  { value: "construction", label: "Why these contracts?" },
  { value: "quality", label: "What should I watch for?" },
  { value: "depth", label: "How many can I buy at this price?" },
];

/** Renders nothing unless the server has Grok configured, so the demo never shows a dead button. */
export default function GrokExplanation({ snapshot }: { snapshot: string }) {
  const [enabled, setEnabled] = useState(false);
  const [topic, setTopic] = useState("construction");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    grokAvailable().then((ok) => alive && setEnabled(ok));
    return () => {
      alive = false;
    };
  }, []);

  async function ask(t: string) {
    setTopic(t);
    setBusy(true);
    setAnswer("");
    setError("");
    try {
      setAnswer((await explainRoute(snapshot, t)).answer);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Explanation unavailable.");
    } finally {
      setBusy(false);
    }
  }

  if (!enabled) return null;
  return (
    <section className="leg space-y-3" aria-label="Ask Grok about this route">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-semibold">Ask Grok about this route</span>
        <span className="muted text-xs">Based on the quotes shown here</span>
      </div>
      <div className="flex flex-wrap gap-2">
        {TOPICS.map((t) => (
          <button key={t.value} type="button" disabled={busy} onClick={() => ask(t.value)} className={`seg text-xs ${topic === t.value && (answer || busy) ? "seg-on" : ""}`}>
            {busy && topic === t.value ? "Asking…" : t.label}
          </button>
        ))}
      </div>
      <div aria-live="polite">
        {answer && <p className="text-sm leading-relaxed">{answer}</p>}
        {error && <p className="note note-warn">{error}</p>}
      </div>
    </section>
  );
}
