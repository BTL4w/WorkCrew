"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { ApiError, isDefinitiveMutationRejection } from "@/shared/api/client";
import { EvidencePicker } from "@/features/daily-updates/evidence-picker";
import { applyBlocker, blockerHistory, listBlockers } from "./api";
import type { Blocker, BlockerCommand } from "./contracts";

type EvidenceRef = { evidence_id: string; version: number };
type Props = {
  taskId: string; taskVersion: number; organizationId: string; actorMembershipId: string;
  commands?: BlockerCommand[]; onCommands?: (commands: BlockerCommand[]) => void;
  availableEvidence?: EvidenceRef[]; disabled?: boolean;
};

export function BlockerPanel(props: Props) {
  const t = useTranslations("blocker");
  const client = useQueryClient();
  const scope = ["work", props.organizationId, props.actorMembershipId, "blockers", props.taskId];
  const query = useQuery({ queryKey: scope, queryFn: () => listBlockers(props.taskId) });
  const [text, setText] = useState("");
  const [severity, setSeverity] = useState<Blocker["severity"]>("MEDIUM");
  const [editing, setEditing] = useState<Blocker | null>(null);
  const [selected, setSelected] = useState<EvidenceRef[]>([]);
  const [uploaded, setUploaded] = useState<EvidenceRef[]>([]);
  const [uploading, setUploading] = useState(false);
  const [historyId, setHistoryId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<{ command: BlockerCommand; key: string } | null>(null);
  const history = useQuery({
    queryKey: [...scope, "history", historyId], queryFn: () => blockerHistory(historyId!), enabled: Boolean(historyId),
  });
  const draftMode = Boolean(props.onCommands);

  async function execute(command: BlockerCommand) {
    if (draftMode) {
      const remaining = (props.commands ?? []).filter(c => !command.blocker_id || c.blocker_id !== command.blocker_id);
      props.onCommands!([...remaining, command]);
      setText(""); setEditing(null); setSelected([]);
      return;
    }
    if (!pending.current) pending.current = { command, key: crypto.randomUUID() };
    setBusy(true); setError(null);
    try {
      await applyBlocker(pending.current.command, pending.current.key);
      pending.current = null; setText(""); setEditing(null); setSelected([]);
      await client.invalidateQueries({ queryKey: scope });
    } catch (failure) {
      const code = failure instanceof ApiError ? failure.code : "UNKNOWN";
      setError(t.has(`error.${code}`) ? t(`error.${code}`) : t("error.UNKNOWN"));
      if (isDefinitiveMutationRejection(failure)) pending.current = null;
    } finally { setBusy(false); setUncertain(Boolean(pending.current)); }
  }
  function command(action: BlockerCommand["action"], blocker?: Blocker): BlockerCommand {
    return {
      task_id: props.taskId, expected_task_version: props.taskVersion, action,
      blocker_id: blocker?.id, expected_blocker_version: blocker?.version,
      text: text || blocker?.text, severity, evidence_refs: selected,
    };
  }
  const blocked = busy || props.disabled || uncertain;
  const refs = Array.from(new Map([...(props.availableEvidence ?? []), ...uploaded, ...(editing?.evidence_refs ?? [])].map(ref => [ref.evidence_id, ref])).values());

  return <section className="mt-6 rounded-2xl border p-4">
    <h3>{t(draftMode ? "reportTitle" : "title")}</h3>
    {error && <p role="alert">{error}</p>}
    {uncertain && <button type="button" disabled={busy} onClick={() => void execute(pending.current!.command)}>{t("retry")}</button>}
    <label>{t("text")}<textarea maxLength={4000} value={text} disabled={blocked} onChange={e => setText(e.target.value)} /></label>
    <label>{t("severity")}<select aria-label={t("severity")} value={severity} disabled={blocked} onChange={e => setSeverity(e.target.value as Blocker["severity"])}>
      {(["LOW", "MEDIUM", "HIGH", "CRITICAL"] as const).map(value => <option key={value} value={value}>{t(`severityValue.${value}`)}</option>)}
    </select></label>
    {refs.map(ref => <label key={ref.evidence_id}>
      <input type="checkbox" disabled={blocked} checked={selected.some(r => r.evidence_id === ref.evidence_id)}
        onChange={e => setSelected(values => e.target.checked ? [...values, ref] : values.filter(r => r.evidence_id !== ref.evidence_id))} />
      {t("evidenceChoice", { id: ref.evidence_id, version: ref.version })}
    </label>)}
    <button type="button" disabled={blocked || !text.trim()} onClick={() => void execute(command(editing ? "EDIT" : "CREATE", editing ?? undefined))}>
      {draftMode ? t("addToReport") : editing ? t("save") : t("create")}
    </button>
    {!draftMode && <button type="button" disabled={blocked} onClick={() => setUploading(v => !v)}>{t("attach")}</button>}
    {uploading && <EvidencePicker onUploaded={proof => setUploaded(values => [...values, { evidence_id: proof.evidence_id, version: proof.version }])} />}
    {(props.commands ?? []).map((c, index) => <p key={index}>{t(`action.${c.action}`)}: {c.text}
      <button type="button" disabled={props.disabled} onClick={() => props.onCommands?.(props.commands!.filter((_, i) => i !== index))}>{t("removeFromReport")}</button>
    </p>)}
    {query.isPending && <p role="status">{t("loading")}</p>}
    {query.isError && <p role="alert">{t("unavailable")} <button type="button" onClick={() => void query.refetch()}>{t("reload")}</button></p>}
    {(query.data ?? []).map(blocker => <article key={blocker.id}>
      <p>{blocker.text} · {t(`status.${blocker.status}`)} · {t(`severityValue.${blocker.severity}`)}{blocker.archived && ` · ${t("archived")}`}</p>
      {blocker.evidence_refs.map(ref => <a key={ref.evidence_id} href={`/api/v1/evidence/${ref.evidence_id}/versions/${ref.version}/content`}>{t("original")} · v{ref.version}</a>)}
      {!blocker.archived && <>
        <button type="button" disabled={blocked} onClick={() => { setEditing(blocker); setText(blocker.text); setSeverity(blocker.severity); setSelected(blocker.evidence_refs); }}>{t("saveEdit")}</button>
        {([...(blocker.status === "OPEN" ? ["ACKNOWLEDGE"] : []), ...(blocker.status === "RESOLVED" ? ["REOPEN"] : ["RESOLVE"]), "ARCHIVE"] as BlockerCommand["action"][]).map(action =>
          <button key={action} type="button" disabled={blocked} onClick={() => void execute(command(action, blocker))}>{t(`action.${action}`)}</button>)}
      </>}
      <button type="button" onClick={() => setHistoryId(blocker.id)}>{t("history")}</button>
    </article>)}
    {historyId && (history.isPending ? <p role="status">{t("loading")}</p> : history.isError ? <p role="alert">{t("unavailable")}</p> : <ol>
      {history.data?.map(event => <li key={event.id}>
        <p>{t(`action.${event.action}`)} · {t(`status.${event.to_status}`)} · {event.at} · v{event.version}</p>
        <p>{event.snapshot.text}</p>
        <p>{t("severity")}: <span>{t(`severityValue.${event.snapshot.severity}`)}</span></p>
        {event.snapshot.evidence_refs.map(ref => <a key={`${ref.evidence_id}:${ref.version}`} href={`/api/v1/evidence/${ref.evidence_id}/versions/${ref.version}/content`}>{t("original")} · v{ref.version}</a>)}
      </li>)}
    </ol>)}
  </section>;
}
