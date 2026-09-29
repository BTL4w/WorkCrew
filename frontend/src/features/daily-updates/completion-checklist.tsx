"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { listAcceptanceCriteria } from "@/features/planning/api";
import { transitionTask } from "@/features/work/api";
import type { Task } from "@/features/work/contracts";
import { ApiError, isDefinitiveMutationRejection } from "@/shared/api/client";
import { getReportingContext } from "./reporting-api";

export function CompletionChecklist({ taskId, version, isEmployee, organizationId,
  actorMembershipId, onCompleted }: {
  taskId: string; version: number; isEmployee: boolean; organizationId: string;
  actorMembershipId: string; onCompleted: (task: Task) => void;
}) {
  const t = useTranslations("completion");
  const scope = ["completion", organizationId, actorMembershipId, taskId];
  const criteria = useQuery({ queryKey: [...scope, "criteria"],
    queryFn: () => listAcceptanceCriteria(taskId) });
  const report = useQuery({ queryKey: [...scope, "report"],
    queryFn: () => getReportingContext(taskId), enabled: isEmployee });
  const [checked, setChecked] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const attempt = useRef<{ fingerprint: string; key: string } | null>(null);
  const readyReport = !isEmployee || (report.data?.reported_percent !== null &&
    Number(report.data?.reported_percent) === 100 && Boolean(report.data?.evidence_refs.length));
  const currentCriteria = criteria.data ?? [];
  const readyCriteria = currentCriteria.every(item => checked[item.id] === item.version);
  const ready = !criteria.isPending && !criteria.error &&
    (!isEmployee || (!report.isPending && !report.error)) &&
    readyReport && readyCriteria;
  async function complete() {
    const attestations = currentCriteria.map(item => ({ criterion_id: item.id, version: item.version,
      confirmed: true, evidence_refs: [] }));
    const fingerprint = JSON.stringify({ taskId, version, attestations });
    if (attempt.current?.fingerprint !== fingerprint) attempt.current = {
      fingerprint, key: crypto.randomUUID(),
    };
    setBusy(true); setError(null);
    try {
      const result = await transitionTask(taskId, "DONE", version, attempt.current.key, attestations);
      attempt.current = null;
      onCompleted(result.data);
    } catch (failure) {
      const code = failure instanceof ApiError ? failure.code : "UNKNOWN";
      setError(t.has(`error.${code}`) ? t(`error.${code}`) : t("error.UNKNOWN"));
      if (isDefinitiveMutationRejection(failure)) attempt.current = null;
    } finally { setBusy(false); }
  }
  return <div className="grid gap-2">
    {isEmployee && report.error && <p role="alert">{t("reportUnavailable")}</p>}
    {isEmployee && !readyReport && !report.isPending && !report.error && <p>{t("reportRequired")} <a
      href="#daily-update-form">{t("openReport")}</a></p>}
    {criteria.error && <p role="alert">{t("criteriaUnavailable")}</p>}
    {currentCriteria.map(item => <label key={item.id} className="block"><input type="checkbox"
      checked={checked[item.id] === item.version}
      onChange={event => setChecked(values => ({ ...values,
        [item.id]: event.target.checked ? item.version : 0 }))} />{item.text}</label>)}
    <button className="primary-button" type="button" disabled={!ready || busy}
      onClick={() => void complete()}>{t("complete")}</button>
    {error && <p role="alert">{error}</p>}
  </div>;
}
