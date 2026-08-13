import { useState } from "react";
import Metrics from "./Metrics";
import Leaderboard from "./Leaderboard";
import RunsLog from "./RunsLog";

type View = "scores" | "leaderboard" | "logs";

const VIEWS: { id: View; label: string; hint: string }[] = [
  { id: "scores", label: "Scores", hint: "Per-arm metrics for one session" },
  { id: "leaderboard", label: "Leaderboard", hint: "Compare across all sessions" },
  { id: "logs", label: "Logs", hint: "Every run + transcript, incl. failures" },
];

export default function Results() {
  const h = window.location.hash;
  const initial: View = h.includes("leaderboard")
    ? "leaderboard"
    : h.includes("logs")
    ? "logs"
    : "scores";
  const [view, setView] = useState<View>(initial);

  return (
    <div className="stack">
      <div className="page-head">
        <h2>Results</h2>
        <p>See how Bito changed the outcome — scores per session and a cross-session leaderboard.</p>
      </div>

      <div className="subtabs">
        {VIEWS.map((v) => (
          <button
            key={v.id}
            className={`subtab${view === v.id ? " active" : ""}`}
            title={v.hint}
            onClick={() => setView(v.id)}
          >
            {v.label}
          </button>
        ))}
      </div>

      {view === "scores" && <Metrics embedded />}
      {view === "leaderboard" && <Leaderboard embedded />}
      {view === "logs" && <RunsLog />}
    </div>
  );
}
