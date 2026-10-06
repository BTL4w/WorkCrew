import { useLocale, useTranslations } from "next-intl";
import type { AssistantBlock } from "../contracts";
import styles from "./report-block.module.css";

type Block = Extract<AssistantBlock,{kind:"report"|"project_status"}>;
export function ReportBlock({block,canManage}:{block:Block;canManage:boolean}) {
 const t=useTranslations("assistant.report");const locale=useLocale();
 if(!canManage) return null;
 // Build the link from validated identities; never navigate to a supplied model href.
 const href=block.kind==="report"?`/?project=${block.project_id}&report=${block.report_id}&version=${block.report_version_id}`:null;
 return <section className={styles.card} aria-label={t(block.kind==="report"?"title":"statusTitle")}>
  <header><p className={styles.eyebrow}>{t(block.kind==="report"?"title":"statusTitle")}</p><h3>{block.project_label}</h3></header>
  <p>{t("period",{start:block.period_start,end:block.period_end,timezone:block.timezone})}</p>
  <p className={styles.muted}>{t("captured",{at:new Intl.DateTimeFormat(locale,{dateStyle:"medium",timeStyle:"short",timeZone:block.timezone}).format(new Date(block.captured_at))})}</p>
  <dl className={styles.metrics}>{block.metrics.map(m=><div key={m.key}><dt>{t.has(`metrics.${m.key}`)?t(`metrics.${m.key}`):m.key}</dt><dd>{m.value??t("unknown")}</dd><p className={styles.muted}>{t(`states.${m.state}`)} · {t.has(`timeBasis.${m.time_basis}`)?t(`timeBasis.${m.time_basis}`):m.time_basis}</p></div>)}</dl>
  {block.kind==="project_status"?<><p className={styles.notice}>{t(block.analysis_state==="VERIFIED"?"analysis":"analysisUnavailable")}</p>{block.analysis.map((text,i)=><p key={i}>{text}</p>)}</>:<p className={styles.notice}>{t("review")}</p>}
  {block.limitations.length>0&&<ul>{block.limitations.map((text,i)=><li key={i}>{text}</li>)}</ul>}
  <details><summary>{t("sources")} · {block.sources.length}</summary><ul>{block.sources.map(s=><li key={`${s.resource_type}:${s.resource_id}`}><span>{s.resource_type} · v{s.version}</span> <code>{s.resource_id}</code></li>)}</ul></details>
  {href&&<><p className={styles.muted}>{t("bounded")}</p><a className="secondary-button" href={href}>{t("open")}</a></>}
 </section>;
}
