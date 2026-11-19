"use client";
import { useState } from "react";
import { explainRoute } from "@/lib/api";

export default function GrokExplanation({ snapshot }: { snapshot: string }) {
  const [topic, setTopic] = useState("construction");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState("");

  async function ask() {
    setBusy(true);
    setAnswer("");
    setError("");
    try {
      setAnswer((await explainRoute(snapshot, topic)).answer);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Explanation unavailable.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="space-y-2 border-t border-line pt-4">
      <label className="block"><span className="label">Ask Grok about this route</span>
        <select className="input" value={topic} disabled={busy} onChange={e => { setTopic(e.target.value); setAnswer(""); setError(""); }}>
          <option value="construction">Why was this construction selected?</option>
          <option value="quality">Which quote warnings matter?</option>
          <option value="depth">What limits the fill?</option>
        </select>
      </label>
      <button type="button" className="btn-outline" onClick={ask} disabled={busy}>{busy ? "Asking Grok…" : "Explain route"}</button>
      <div aria-live="polite">{answer && <p className="text-sm">{answer}</p>}{error && <p className="note note-warn">{error}</p>}</div>
    </section>
  );
}
