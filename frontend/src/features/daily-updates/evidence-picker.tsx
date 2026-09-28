"use client";

import { useTranslations } from "next-intl";
import { useId, useRef, useState } from "react";
import { ApiError, isDefinitiveMutationRejection } from "@/shared/api/client";
import { evidenceDownloadUrl, uploadEvidence } from "./api";
import { MAX_EVIDENCE_BYTES, evidenceTypes, type Evidence } from "./contracts";

export function EvidencePicker({ onUploaded }: { onUploaded?: (evidence: Evidence) => void }) {
  const t = useTranslations("evidence");
  const inputId = useId();
  const [originals, setOriginals] = useState<Evidence[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const retry = useRef<{ file: File; key: string } | null>(null);
  const [canRetry, setCanRetry] = useState(false);

  async function submit(file: File, key: string) {
    setPending(true); setError(null); setCanRetry(false);
    retry.current = { file, key };
    try {
      const original = await uploadEvidence(file, key);
      setOriginals((items) => items.some((item) => item.evidence_id === original.evidence_id)
        ? items : [...items, original]);
      retry.current = null;
      onUploaded?.(original);
    } catch (failure) {
      const code = failure instanceof ApiError ? failure.code : "EVIDENCE_STORAGE_UNAVAILABLE";
      setError(t.has(`error.${code}`) ? t(`error.${code}`) : t("error.EVIDENCE_STORAGE_UNAVAILABLE"));
      setCanRetry(!isDefinitiveMutationRejection(failure) || code === "EVIDENCE_UPLOAD_IN_PROGRESS");
    } finally { setPending(false); }
  }

  function select(file: File | undefined) {
    setError(null); setCanRetry(false);
    if (!file) return;
    if (file.size > MAX_EVIDENCE_BYTES) { setError(t("error.EVIDENCE_TOO_LARGE")); return; }
    const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
    if (!evidenceTypes[extension]) { setError(t("error.EVIDENCE_INVALID_FORMAT")); return; }
    void submit(file, crypto.randomUUID());
  }

  return <section className="mt-6 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-5">
    <h2 className="text-lg font-semibold">{t("title")}</h2>
    <p className="mt-2 text-sm text-slate-600">{t("description")}</p>
    <label htmlFor={inputId} className="mt-4 block font-medium">{t("choose")}</label>
    <input id={inputId} type="file" accept=".pdf,.docx,.jpg,.jpeg,.png" disabled={pending}
      onChange={(event) => { select(event.target.files?.[0]); event.target.value = ""; }} />
    {pending && <p role="status">{t("uploading")}</p>}
    {error && <p role="alert" className="mt-2 text-red-700">{error}</p>}
    {canRetry && <button type="button" disabled={pending} onClick={() => {
      if (retry.current) void submit(retry.current.file, retry.current.key);
    }}>{t("retry")}</button>}
    <ul className="mt-4 space-y-2">{originals.map((original) => <li key={original.evidence_id}>
      <span>{original.filename} · {original.mime_type} · {original.byte_length} {t("bytes")}</span>{" "}
      <a href={evidenceDownloadUrl(original)} download>{t("download")}</a>
    </li>)}</ul>
  </section>;
}
