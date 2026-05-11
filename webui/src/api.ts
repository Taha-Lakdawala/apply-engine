export type Stats = {
  total_applications: number;
  by_status: Record<string, number>;
  submitted: number;
  failed: number;
  filled: number;
  per_day: { day: string; count: number }[];
  top_companies: { company: string; count: number }[];
  questions_total: number;
  answers_total: number;
  ai_unreviewed: number;
};

export type ApplicationRow = {
  id: number;
  url: string;
  company: string | null;
  job_title: string | null;
  raw_title: string | null;
  status: string;
  submitted_at: string | null;
  created_at: string;
  error: string | null;
};

export type QA = {
  question_id: number;
  answer_id: number;
  question: string;
  field_type: string;
  options: string[] | null;
  value: string;
  ai_generated: boolean;
  reviewed_at: string | null;
  created_at: string;
};

export type ApplicationDetail = ApplicationRow & {
  screenshots: { label: string; path: string }[];
  qa: QA[];
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json();
}

export const api = {
  stats: () => req<Stats>("/api/stats"),
  applications: (status?: string) =>
    req<ApplicationRow[]>(`/api/applications${status ? `?status=${status}` : ""}`),
  application: (id: number) => req<ApplicationDetail>(`/api/applications/${id}`),
  questions: () => req<QA[]>("/api/questions"),
  profile: () => req<{ data: Record<string, unknown>; exists: boolean; path: string }>("/api/profile"),
  saveProfile: (data: Record<string, unknown>) =>
    req<{ ok: boolean }>("/api/profile", { method: "PUT", body: JSON.stringify({ data }) }),
  updateAnswer: (questionId: number, value: string) =>
    req<{ ok: boolean }>(`/api/answers/${questionId}`, { method: "PUT", body: JSON.stringify({ value }) }),
  deleteQuestion: (questionId: number) =>
    req<{ ok: boolean }>(`/api/questions/${questionId}`, { method: "DELETE" }),
};

export function screenshotUrl(path: string): string {
  return `/api/screenshots/${path}`;
}
