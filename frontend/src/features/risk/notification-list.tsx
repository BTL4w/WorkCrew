"use client";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { isDefinitiveMutationRejection } from "@/shared/api/client";
import { notifications, markRead } from "./api";
export function NotificationList(props: { organizationId: string; actorMembershipId: string; onOpenTask?: (id: string) => void | Promise<void> }) {
 const t = useTranslations("risk"); const client = useQueryClient();
 const scope = ["work", props.organizationId, props.actorMembershipId, "notifications"];
 const [open, setOpen] = useState(false);
 const query = useQuery({ queryKey: scope, queryFn: notifications, enabled: open, refetchInterval: 30000 });
 const pending = useRef<null | { id: string; key: string }>(null);
 const [busy, setBusy] = useState(false); const [error, setError] = useState(false); const [uncertain, setUncertain] = useState(false);
 async function read(id: string) {
  if (!pending.current) pending.current = { id, key: crypto.randomUUID() }; setBusy(true); setError(false);
  try { await markRead(pending.current.id, pending.current.key); pending.current = null; await client.invalidateQueries({ queryKey: scope }); }
  catch (failure) { setError(true); if (isDefinitiveMutationRejection(failure)) pending.current = null; }
  finally { setBusy(false); setUncertain(Boolean(pending.current)); }
 }
 return <details onToggle={e => setOpen(e.currentTarget.open)} className="mb-6 rounded-2xl border p-4"><summary>{t("notifications")}{query.data && ` (${query.data.filter(n => !n.read).length})`}</summary>
  {query.isPending && <p role="status">{t("loading")}</p>}{(query.isError || error) && <p role="alert">{t("error")}<button onClick={() => void query.refetch()}>{t("reload")}</button></p>}
  {uncertain && <button disabled={busy} onClick={() => void read(pending.current!.id)}>{t("retry")}</button>}
  {query.data?.length === 0 && <p>{t("noNotifications")}</p>}
  <ul>{query.data?.map(notice => <li key={notice.id}>{t(`notificationKind.${notice.kind}`)} · {notice.created_at}
   {props.onOpenTask && <button onClick={() => { void Promise.resolve(props.onOpenTask!(notice.task_id)).catch(() => setError(true)); }}>{t("openTask")}</button>}
   {!notice.read && <button disabled={busy || uncertain} onClick={() => void read(notice.id)}>{t("markRead")}</button>}
  </li>)}</ul>
 </details>;
}
