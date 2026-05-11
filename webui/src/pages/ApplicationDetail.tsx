import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, screenshotUrl, type ApplicationDetail as Detail } from "../api";
import { relTime } from "./Dashboard";

export default function ApplicationDetail() {
  const { id } = useParams();
  const [data, setData] = useState<Detail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [zoom, setZoom] = useState<string | null>(null);
  const [zoomed, setZoomed] = useState(false);

  useEffect(() => {
    if (!id) return;
    api.application(Number(id)).then(setData).catch((e) => setErr(String(e)));
  }, [id]);

  useEffect(() => {
    if (!zoom) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setZoom(null);
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [zoom]);

  const openZoom = (url: string) => {
    setZoom(url);
    setZoomed(false);
  };

  if (err) return <div className="flash err">{err}</div>;
  if (!data) return <div className="empty">Loading…</div>;

  return (
    <>
      <p style={{ margin: "0 0 12px" }}>
        <Link to="/applications">← All applications</Link>
      </p>
      <h1 className="page-title">{data.job_title || data.raw_title || "Application"}</h1>
      <p className="page-sub">
        {data.company || "Unknown company"} ·{" "}
        <span className={"pill " + data.status}>{data.status}</span> ·{" "}
        {relTime(data.submitted_at || data.created_at)}
      </p>

      {data.error && <div className="flash err">Error: {data.error}</div>}

      <div className="panel">
        <div className="panel-title">Job URL</div>
        <a href={data.url} target="_blank" rel="noreferrer" style={{ wordBreak: "break-all" }}>{data.url}</a>
      </div>

      <div className="detail-grid">
        <div>
          <div className="panel">
            <div className="panel-title">Questions & answers ({data.qa.length})</div>
            {data.qa.length === 0 ? (
              <div className="empty" style={{ padding: 20 }}>
                No Q&A recorded for this application URL.
              </div>
            ) : (
              data.qa.map((qa) => (
                <div key={qa.answer_id} className="qa-item">
                  <div className="qa-question">{qa.question}</div>
                  <div className="qa-meta">
                    {qa.field_type}
                    {qa.ai_generated ? " · AI" : " · manual"}
                    {qa.reviewed_at ? " · reviewed" : ""}
                    {" · "}
                    {relTime(qa.created_at)}
                  </div>
                  <div className="qa-answer">{qa.value || <em style={{ color: "var(--muted)" }}>(empty)</em>}</div>
                </div>
              ))
            )}
          </div>
        </div>

        <div>
          <div className="panel">
            <div className="panel-title">Screenshots ({data.screenshots.length})</div>
            {data.screenshots.length === 0 ? (
              <div className="empty" style={{ padding: 20 }}>None for this application.</div>
            ) : (
              <div className="screenshots">
                {data.screenshots.map((s) => (
                  <div key={s.path} className="screenshot">
                    <img src={screenshotUrl(s.path)} alt={s.label} onClick={() => openZoom(screenshotUrl(s.path))} />
                    <div className="screenshot-label">{s.label}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {zoom && (
        <div className="lightbox" onClick={() => setZoom(null)}>
          <button
            className="lightbox-close"
            onClick={(e) => { e.stopPropagation(); setZoom(null); }}
            aria-label="Close"
          >
            ×
          </button>
          <img
            src={zoom}
            alt="screenshot"
            className={zoomed ? "zoomed" : ""}
            onClick={(e) => { e.stopPropagation(); setZoomed((z) => !z); }}
          />
        </div>
      )}
    </>
  );
}
