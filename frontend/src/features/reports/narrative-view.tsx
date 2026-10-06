"use client";
import { useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { isDefinitiveMutationRejection } from "@/shared/api/client";
import { generateNarrative } from "./api";
import type { ReportResult } from "./contracts";
import {NarrativeEditor} from "./narrative-editor";
import styles from "./reports.module.css";

export function NarrativeView({data,onUpdated,onStale,generate=generateNarrative}:{data:ReportResult;onUpdated:(data:ReportResult)=>void;onStale:()=>void;generate?:typeof generateNarrative}) {
  const t=useTranslations("reports");
  const [pending,setPending]=useState(false);
  const [error,setError]=useState(false);
  const [stale,setStale]=useState(false);
  const intent=useRef<{binding:string;key:string}|null>(null);
  const state=data.generation_state;
  async function retry() {
    if(pending||stale) return;
    const body={base_version_id:data.selected_version.id,snapshot_hash:data.snapshot.snapshot_hash};
    const binding=JSON.stringify([data.report.id,data.report.version,body]);
    if(intent.current?.binding!==binding) intent.current={binding,key:crypto.randomUUID()};
    setPending(true);setError(false);
    try {onUpdated(await generate(data.report.id,body,data.report.version,intent.current.key));intent.current=null;}
    catch(cause) {setError(true);if(isDefinitiveMutationRejection(cause)) intent.current=null;
      if(typeof cause==="object"&&cause!==null&&"status" in cause&&cause.status===412){setStale(true);onStale();}}
    finally {setPending(false);}
  }
  return <section aria-label={t("narrativeTitle")}>
    <h4 className={styles.groupTitle}>{t("narrativeTitle")}</h4>
    <p role="status" className={styles.notice}>{t(data.review_state&&data.review_state!=="PENDING"&&!["QUEUED","RUNNING"].includes(state)?`review.${data.review_state}`:`generation.${state}`)}</p>
    {data.narrative_access_state==="UNAVAILABLE"&&<p role="alert">{t("narrativeUnavailable")}</p>}
    {data.selected_version.narrative && <div>
      <p className={styles.description}>{t(data.verification_state==="PENDING"||data.verification_state==="FAILED"?`verification.${data.verification_state}`:data.review_state&&data.review_state!=="PENDING"?`review.${data.review_state}`:"draftNotice")}</p>
      {data.selected_version.narrative.blocks.map(block=><div key={block.id}>
        {data.selected_version.block_origins?.[block.id]==="HUMAN"&&<span className={styles.badge}>{t("humanAuthored")}</span>}
        <p>{block.kind==="FACT"?data.selected_version.rendered_facts?.[block.id]??t("unknown"):block.text}</p>
        {block.kind!=="FACT"&&<><p className={styles.description}>{t(`blockKind.${block.kind}`)}</p>
          {block.assumptions.map((assumption,i)=><p key={i}>{t("assumption",{text:assumption})}</p>)}
          {block.source_refs.map(source=><p key={`${source.resource_type}:${source.resource_id}:${source.version}`} className={styles.sourceId}>{source.resource_type} · {source.resource_id} · {t("version",{version:source.version})}</p>)}</>}
        {block.kind==="FACT"&&<>{block.bindings.map(binding=><p key={binding.metric_key} className={styles.sourceId}>{binding.metric_key}</p>)}
          {block.source_bindings?.map(source=><p key={`${source.resource_type}:${source.resource_id}:${source.version}`} className={styles.sourceId}>{source.resource_type} · {source.resource_id} · {t("version",{version:source.version})}</p>)}</>}
      </div>)}
      <details><summary>{t("snapshotReceipt")}</summary><p className={styles.sourceId}>{data.snapshot.snapshot_hash}</p></details>
    </div>}
    {data.selected_version.narrative&&<NarrativeEditor key={data.selected_version.id} data={data} onUpdated={onUpdated} onStale={onStale}/>}
    {(["NOT_REQUESTED","AI_UNAVAILABLE","FAILED"].includes(state)||data.review_state==="REJECTED"||data.review_state==="ACCEPTED")&&!["QUEUED","RUNNING"].includes(state)&&<button className="secondary-button" type="button" disabled={pending||stale} onClick={()=>void retry()}>{t(pending?"generationSubmitting":"retryNarrative")}</button>}
    {error&&<p role="alert">{t(stale?"generationStale":"generationError")}</p>}
  </section>;
}
