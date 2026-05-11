import { useEffect, useMemo, useState } from "react";
import { api, type QA } from "../api";
import { relTime } from "./Dashboard";

export default function Questions() {
  const [items, setItems] = useState<QA[]>([]);
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<"all" | "ai" | "manual" | "unreviewed">("all");
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const load = () => api.questions().then(setItems).catch((e) => setErr(String(e)));
  useEffect(() => { load(); }, []);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return items.filter((x) => {
      if (filter === "ai" && !x.ai_generated) return false;
      if (filter === "manual" && x.ai_generated) return false;
      if (filter === "unreviewed" && (!x.ai_generated || x.reviewed_at)) return false;
      if (needle && !`${x.question} ${x.value}`.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [items, q, filter]);

  const save = async (qid: number) => {
    try {
      await api.updateAnswer(qid, draft);
      setEditing(null);
      setDraft("");
      await load();
    } catch (e) {
      setErr(String(e));
    }
  };

  const remove = async (qid: number, question: string) => {
    const preview = question.length > 80 ? question.slice(0, 80) + "…" : question;
    if (!window.confirm(`Delete cached answer for:\n\n${preview}\n\nThis removes the question and its answer history. The next application that asks it will get a fresh answer.`)) {
      return;
    }
    try {
      await api.deleteQuestion(qid);
      await load();
    } catch (e) {
      setErr(String(e));
    }
  };

  return (
    <>
      <h1 className="page-title">Questions</h1>
      <p className="page-sub">{items.length} stored · showing {filtered.length}</p>

      {err && <div className="flash err">{err}</div>}

      <div className="toolbar">
        <select value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)}>
          <option value="all">All</option>
          <option value="ai">AI-generated</option>
          <option value="manual">Manual</option>
          <option value="unreviewed">AI · unreviewed</option>
        </select>
        <input
          className="search-input"
          placeholder="Search question or answer…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 50 }}>#</th>
              <th>Question</th>
              <th>Answer</th>
              <th style={{ width: 110 }}>Source</th>
              <th style={{ width: 110 }}>Updated</th>
              <th style={{ width: 160 }}></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((qa) => (
              <tr key={qa.question_id}>
                <td style={{ color: "var(--muted)" }}>{qa.question_id}</td>
                <td style={{ maxWidth: 320 }}>{qa.question}</td>
                <td style={{ maxWidth: 360 }}>
                  {editing === qa.question_id ? (
                    <textarea
                      autoFocus
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      rows={3}
                    />
                  ) : (
                    <div style={{ whiteSpace: "pre-wrap" }}>{qa.value}</div>
                  )}
                </td>
                <td>
                  <span className="pill" style={{ background: qa.ai_generated ? "rgba(139,92,246,.15)" : "rgba(34,197,94,.15)", color: qa.ai_generated ? "#a78bfa" : "#4ade80" }}>
                    {qa.ai_generated ? (qa.reviewed_at ? "AI ✓" : "AI") : "manual"}
                  </span>
                </td>
                <td>{relTime(qa.created_at)}</td>
                <td>
                  {editing === qa.question_id ? (
                    <div style={{ display: "flex", gap: 6 }}>
                      <button onClick={() => save(qa.question_id)}>Save</button>
                      <button className="ghost" onClick={() => setEditing(null)}>Cancel</button>
                    </div>
                  ) : (
                    <div style={{ display: "flex", gap: 6 }}>
                      <button className="ghost" onClick={() => { setEditing(qa.question_id); setDraft(qa.value); }}>Edit</button>
                      <button className="ghost danger" onClick={() => remove(qa.question_id, qa.question)}>Delete</button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
