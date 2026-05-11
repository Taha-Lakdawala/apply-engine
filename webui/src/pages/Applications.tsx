import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type ApplicationRow } from "../api";
import { relTime } from "./Dashboard";

export default function Applications() {
  const [rows, setRows] = useState<ApplicationRow[]>([]);
  const [status, setStatus] = useState<string>("");
  const [q, setQ] = useState("");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.applications(status || undefined).then(setRows).catch((e) => setErr(String(e)));
  }, [status]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((r) =>
      [r.company, r.job_title, r.url].some((v) => (v || "").toLowerCase().includes(needle))
    );
  }, [rows, q]);

  return (
    <>
      <h1 className="page-title">Applications</h1>
      <p className="page-sub">{rows.length} total · showing {filtered.length}</p>

      {err && <div className="flash err">{err}</div>}

      <div className="toolbar">
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          <option value="submitted">submitted</option>
          <option value="failed">failed</option>
          <option value="filled">filled</option>
        </select>
        <input
          className="search-input"
          placeholder="Search company, role, URL…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 50 }}>#</th>
              <th>Company</th>
              <th>Role</th>
              <th style={{ width: 110 }}>Status</th>
              <th style={{ width: 130 }}>When</th>
              <th style={{ width: 80 }}></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <tr key={r.id}>
                <td style={{ color: "var(--muted)" }}>{r.id}</td>
                <td>{r.company || "—"}</td>
                <td>{r.job_title || "—"}</td>
                <td><span className={"pill " + r.status}>{r.status}</span></td>
                <td title={r.created_at}>{relTime(r.submitted_at || r.created_at)}</td>
                <td><Link to={`/applications/${r.id}`}>Open</Link></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
