import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Stats, type ApplicationRow } from "../api";

export default function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [recent, setRecent] = useState<ApplicationRow[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.stats(), api.applications()])
      .then(([s, a]) => { setStats(s); setRecent(a.slice(0, 8)); })
      .catch((e) => setErr(String(e)));
  }, []);

  if (err) return <div className="flash err">{err}</div>;
  if (!stats) return <div className="empty">Loading…</div>;

  const maxDay = Math.max(1, ...stats.per_day.map((d) => d.count));
  const lastDays = stats.per_day.slice(-30);

  return (
    <>
      <h1 className="page-title">Overview</h1>
      <p className="page-sub">Application activity, success rate, and answer cache.</p>

      <div className="cards">
        <Card label="Total" value={stats.total_applications} />
        <Card label="Submitted" value={stats.submitted} accent="green" />
        <Card label="Failed" value={stats.failed} accent="red" />
        <Card label="Filled (no submit)" value={stats.filled} accent="amber" />
        <Card label="Cached questions" value={stats.questions_total} sub={`${stats.answers_total} answers`} />
        <Card label="AI to review" value={stats.ai_unreviewed} sub="not yet confirmed" />
      </div>

      <div className="panel">
        <div className="panel-title">Activity (last 30 days)</div>
        {lastDays.length === 0 ? (
          <div className="empty" style={{ padding: 20 }}>No activity yet.</div>
        ) : (
          <div className="bars">
            {lastDays.map((d) => (
              <div
                key={d.day}
                className="bar"
                style={{ height: `${(d.count / maxDay) * 100}%` }}
                title={`${d.day} — ${d.count}`}
              />
            ))}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-title">
          <span>Recent applications</span>
          <Link to="/applications">View all →</Link>
        </div>
        <ApplicationsTable rows={recent} />
      </div>

      <div className="panel">
        <div className="panel-title">Top companies (submitted)</div>
        {stats.top_companies.length === 0 ? (
          <div className="empty" style={{ padding: 20 }}>None yet.</div>
        ) : (
          <table>
            <thead><tr><th>Company</th><th style={{ width: 80 }}>Apps</th></tr></thead>
            <tbody>
              {stats.top_companies.map((c) => (
                <tr key={c.company}><td>{c.company}</td><td>{c.count}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function Card({ label, value, sub, accent }: { label: string; value: number | string; sub?: string; accent?: "green" | "red" | "amber" }) {
  const color = accent === "green" ? "var(--green)" : accent === "red" ? "var(--red)" : accent === "amber" ? "var(--amber)" : undefined;
  return (
    <div className="card">
      <div className="card-label">{label}</div>
      <div className="card-value" style={color ? { color } : undefined}>{value}</div>
      {sub && <div className="card-sub">{sub}</div>}
    </div>
  );
}

export function ApplicationsTable({ rows }: { rows: ApplicationRow[] }) {
  if (rows.length === 0) return <div className="empty" style={{ padding: 20 }}>No applications yet.</div>;
  return (
    <table>
      <thead>
        <tr>
          <th>Company</th>
          <th>Role</th>
          <th>Status</th>
          <th>When</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td>{r.company || "—"}</td>
            <td>{r.job_title || "—"}</td>
            <td><span className={"pill " + r.status}>{r.status}</span></td>
            <td title={r.created_at}>{relTime(r.submitted_at || r.created_at)}</td>
            <td><Link to={`/applications/${r.id}`}>Details →</Link></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function relTime(iso: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  const diff = Date.now() - t;
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(iso).toLocaleDateString();
}
