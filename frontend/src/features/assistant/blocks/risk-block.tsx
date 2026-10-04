import { useId, useRef } from "react";
import { useTranslations } from "next-intl";
import type { AssistantBlock } from "../contracts";
import styles from "./risk-block.module.css";

type Block = Extract<AssistantBlock, {kind:"risk"}>;
export function RiskBlock({block}:{block:Block}) {
 const t=useTranslations("risk.chat");
 const data=block.content;
 const recommendations=[...new Set([...data.recommendations,...data.explanation.recommendations])];
 const limitations=[...new Set([...data.limitations,...data.explanation.limitations])];
 const sourceNamespace=useId();
 const details=useRef<HTMLDetailsElement>(null);
 const sourceLabel=(id:string)=>{const source=data.permitted_sources.find(item=>item.id===id);return source && t.has(`sourceTypes.${source.kind}`)?t(`sourceTypes.${source.kind}`):t("sources");};
 const title=data.permitted_sources.find(source=>source.kind==="TASK")?.values.title;
 return <section className={styles.card} aria-label={t("title")}>
  <header className={styles.header}>
   <div><p className={styles.eyebrow}>{t("title")}</p><h3>{typeof title==="string"?title:t("title")}</h3><p className={styles.scope}>{t(data.scope==="OWN_WORK"?"ownScope":"managerScope")}</p></div>
   <div className={styles.score} data-band={data.band??"UNKNOWN"}>
    <strong>{data.score??t("unknown")}</strong>{data.score!==null?<span>/100</span>:null}
    <span className={styles.band}>{data.band?t(`bands.${data.band}`):t(`states.${data.state}`)}</span>
   </div>
  </header>
  <p className={styles.caption}>{data.score!==null?t("scoreOrigin"):t(`states.${data.state}`)}</p>
  {data.fallback?<p className={styles.notice}>{t("fallback")}</p>:null}
  {data.rationale?<p className={styles.rationale}>{data.rationale}</p>:null}
  {data.explanation.observation_explanations.length>0?<div className={styles.section}><h4>{t("reasons")}</h4>
   {data.explanation.observation_explanations.map((item,index)=><div className={styles.reason} key={index}><p>{item.text}</p><div className={styles.references}>{item.source_ids.map(id=><a key={id} onClick={()=>{if(details.current) details.current.open=true;}} href={`#risk-source-${sourceNamespace}-${encodeURIComponent(id)}`}>{sourceLabel(id)}</a>)}</div></div>)}
  </div>:null}
  {recommendations.length>0?<div className={styles.actions}><h4>{t("actions")}</h4><ul>{recommendations.map((item,index)=><li key={index}>{item}</li>)}</ul><p>{t("advisory")}</p></div>:null}
  {limitations.length>0?<div className={styles.section}><h4>{t("limitations")}</h4><ul>{limitations.map((item,index)=><li key={index}>{t.has(`limitationCodes.${item}`)?t(`limitationCodes.${item}`):item}</li>)}</ul></div>:null}
  {data.explanation.replan_requested?<p className={styles.notice}>{t("replan")}</p>:null}
  <details className={styles.sources} ref={details}><summary>{t("sources")} <span>{data.permitted_sources.length}</span></summary><dl>{data.permitted_sources.map(source=><div key={source.id} id={`risk-source-${sourceNamespace}-${encodeURIComponent(source.id)}`}><dt>{source.id}</dt><dd>{Object.entries(source.values).filter(([field])=>t.has(`fields.${field}`)).map(([field,value])=><p key={field}><span>{t(`fields.${field}`)}: </span>{value===null?t("unknown"):typeof value==="boolean"?t(value?"yes":"no"):Array.isArray(value)?t("warningCount",{count:value.length}):typeof value==="string" && t.has(`values.${value}`)?t(`values.${value}`):typeof value==="object"?t("unknown"):String(value)}</p>)}</dd></div>)}</dl></details>
 </section>;
}
