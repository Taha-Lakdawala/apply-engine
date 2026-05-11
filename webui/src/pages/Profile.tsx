import { useEffect, useState } from "react";
import { api } from "../api";

type Section = Record<string, unknown>;

export default function Profile() {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [path, setPath] = useState<string>("");
  const [err, setErr] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.profile()
      .then((p) => { setData(p.data || {}); setPath(p.path); })
      .catch((e) => setErr(String(e)));
  }, []);

  const setSection = (key: string, value: Section) => {
    setData((d) => ({ ...(d || {}), [key]: value }));
  };

  const save = async () => {
    if (!data) return;
    setSaving(true);
    setErr(null);
    setFlash(null);
    try {
      await api.saveProfile(data);
      setFlash("Saved. profile.yaml updated (a timestamped backup was written next to it).");
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  };

  if (err && !data) return <div className="flash err">{err}</div>;
  if (!data) return <div className="empty">Loading…</div>;

  const personal = (data.personal as Section) || {};
  const location = (data.location as Section) || {};
  const links = (data.links as Section) || {};
  const workAuth = (data.work_authorization as Section) || {};
  const demo = (data.demographics as Section) || {};
  const presets = (data.preset_answers as Record<string, string>) || {};
  const bio = (data.bio as string) || "";
  const resumePath = (data.resume_path as string) || "";

  return (
    <>
      <h1 className="page-title">Profile</h1>
      <p className="page-sub" style={{ wordBreak: "break-all" }}>{path}</p>

      {flash && <div className="flash ok">{flash}</div>}
      {err && <div className="flash err">{err}</div>}

      <div className="panel">
        <div className="panel-title">Personal</div>
        <Grid>
          <Field label="First name" value={personal.first_name} onChange={(v) => setSection("personal", { ...personal, first_name: v })} />
          <Field label="Last name" value={personal.last_name} onChange={(v) => setSection("personal", { ...personal, last_name: v })} />
          <Field label="Preferred name" value={personal.preferred_name} onChange={(v) => setSection("personal", { ...personal, preferred_name: v })} />
          <Field label="Pronouns" value={personal.pronouns} onChange={(v) => setSection("personal", { ...personal, pronouns: v })} />
          <Field label="Email" value={personal.email} onChange={(v) => setSection("personal", { ...personal, email: v })} />
          <Field label="Phone" value={personal.phone} onChange={(v) => setSection("personal", { ...personal, phone: v })} />
        </Grid>
      </div>

      <div className="panel">
        <div className="panel-title">Location</div>
        <Grid>
          <Field label="City" value={location.city} onChange={(v) => setSection("location", { ...location, city: v })} />
          <Field label="State" value={location.state} onChange={(v) => setSection("location", { ...location, state: v })} />
          <Field label="Country" value={location.country} onChange={(v) => setSection("location", { ...location, country: v })} />
          <Field label="Postal code" value={location.postal_code} onChange={(v) => setSection("location", { ...location, postal_code: v })} />
        </Grid>
      </div>

      <div className="panel">
        <div className="panel-title">Links</div>
        <Grid>
          <Field label="LinkedIn" value={links.linkedin} onChange={(v) => setSection("links", { ...links, linkedin: v })} />
          <Field label="GitHub" value={links.github} onChange={(v) => setSection("links", { ...links, github: v })} />
          <Field label="Website" value={links.website} onChange={(v) => setSection("links", { ...links, website: v })} />
          <Field label="Portfolio" value={links.portfolio} onChange={(v) => setSection("links", { ...links, portfolio: v })} />
          <Field label="Twitter" value={links.twitter} onChange={(v) => setSection("links", { ...links, twitter: v })} />
        </Grid>
      </div>

      <div className="panel">
        <div className="panel-title">Work authorization</div>
        <Grid>
          <BoolField
            label="Authorized to work"
            value={!!workAuth.authorized_to_work}
            onChange={(v) => setSection("work_authorization", { ...workAuth, authorized_to_work: v })}
          />
          <BoolField
            label="Requires sponsorship"
            value={!!workAuth.requires_sponsorship}
            onChange={(v) => setSection("work_authorization", { ...workAuth, requires_sponsorship: v })}
          />
          <Field
            label="Visa status"
            value={workAuth.visa_status}
            onChange={(v) => setSection("work_authorization", { ...workAuth, visa_status: v })}
          />
        </Grid>
      </div>

      <div className="panel">
        <div className="panel-title">Demographics (EEO)</div>
        <Grid>
          <Field label="Gender" value={demo.gender} onChange={(v) => setSection("demographics", { ...demo, gender: v })} />
          <Field label="Race / ethnicity" value={demo.race_ethnicity} onChange={(v) => setSection("demographics", { ...demo, race_ethnicity: v })} />
          <Field label="Veteran status" value={demo.veteran_status} onChange={(v) => setSection("demographics", { ...demo, veteran_status: v })} />
          <Field label="Disability status" value={demo.disability_status} onChange={(v) => setSection("demographics", { ...demo, disability_status: v })} />
          <Field label="Hispanic or Latino" value={demo.hispanic_or_latino} onChange={(v) => setSection("demographics", { ...demo, hispanic_or_latino: v })} />
        </Grid>
      </div>

      <div className="panel">
        <div className="panel-title">Bio</div>
        <textarea
          rows={8}
          value={bio}
          onChange={(e) => setData({ ...data, bio: e.target.value })}
          placeholder="Free-form context for AI-generated answers."
        />
      </div>

      <div className="panel">
        <div className="panel-title">Resume</div>
        <Field label="Path (relative to repo root or absolute)" value={resumePath} onChange={(v) => setData({ ...data, resume_path: v })} />
      </div>

      <PresetEditor
        value={presets}
        onChange={(v) => setData({ ...data, preset_answers: v })}
      />

      <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 6 }}>
        <button onClick={save} disabled={saving}>{saving ? "Saving…" : "Save profile.yaml"}</button>
      </div>
    </>
  );
}

function Grid({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>{children}</div>;
}

function Field({ label, value, onChange }: { label: string; value: unknown; onChange: (v: string) => void }) {
  return (
    <div className="field">
      <label>{label}</label>
      <input value={(value as string) ?? ""} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

function BoolField({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="field">
      <label>{label}</label>
      <select value={value ? "yes" : "no"} onChange={(e) => onChange(e.target.value === "yes")}>
        <option value="yes">Yes</option>
        <option value="no">No</option>
      </select>
    </div>
  );
}

function PresetEditor({ value, onChange }: { value: Record<string, string>; onChange: (v: Record<string, string>) => void }) {
  const entries = Object.entries(value || {});
  const [newKey, setNewKey] = useState("");
  const [newVal, setNewVal] = useState("");

  const update = (oldKey: string, key: string, val: string) => {
    const copy: Record<string, string> = {};
    for (const [k, v] of entries) {
      if (k === oldKey) copy[key] = val;
      else copy[k] = v as string;
    }
    onChange(copy);
  };
  const remove = (key: string) => {
    const copy = { ...value };
    delete copy[key];
    onChange(copy);
  };
  const add = () => {
    if (!newKey.trim()) return;
    onChange({ ...value, [newKey.trim()]: newVal });
    setNewKey(""); setNewVal("");
  };

  return (
    <div className="panel">
      <div className="panel-title">Preset answers</div>
      <p className="page-sub" style={{ marginTop: -8 }}>Exact-match question → answer. Beats AI on the next run.</p>
      {entries.length === 0 && <div className="empty" style={{ padding: 14, marginBottom: 12 }}>None yet.</div>}
      {entries.map(([k, v]) => (
        <div key={k} style={{ display: "grid", gridTemplateColumns: "1fr 1fr 80px", gap: 8, marginBottom: 8 }}>
          <input defaultValue={k} onBlur={(e) => update(k, e.target.value, v as string)} placeholder="Question" />
          <input defaultValue={v as string} onBlur={(e) => update(k, k, e.target.value)} placeholder="Answer" />
          <button className="ghost" onClick={() => remove(k)}>Remove</button>
        </div>
      ))}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 80px", gap: 8 }}>
        <input value={newKey} onChange={(e) => setNewKey(e.target.value)} placeholder="New question…" />
        <input value={newVal} onChange={(e) => setNewVal(e.target.value)} placeholder="Answer…" />
        <button onClick={add}>Add</button>
      </div>
    </div>
  );
}
