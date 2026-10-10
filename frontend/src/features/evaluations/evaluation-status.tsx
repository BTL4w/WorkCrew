"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { getEvaluation, startEvaluation } from "./api";
import { evaluationRequestSchema, type EvaluationRun } from "./contracts";

type Props={organizationId:string;membershipId:string;role:"ADMIN"|"MANAGER"|"EMPLOYEE"};
export function EvaluationStatus(props:Props){
 if(props.role!=="ADMIN") return null;
 return <AdminStatus key={`${props.organizationId}:${props.membershipId}:${props.role}`} {...props}/>;
}
function AdminStatus({organizationId,membershipId}:Props){
 const t=useTranslations("evaluations");
 const client=useQueryClient();
 const [dataset,setDataset]=useState("");const [provider,setProvider]=useState<"mock"|"hosted">("mock");
 const [openId,setOpenId]=useState("");const [runId,setRunId]=useState("");
 const [started,setStarted]=useState<EvaluationRun|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState(false);
 const attempt=useRef<{fingerprint:string;key:string}|null>(null);
 useEffect(()=>()=>{void client.cancelQueries({queryKey:["evaluations",organizationId,membershipId]});client.removeQueries({queryKey:["evaluations",organizationId,membershipId]});},[client,organizationId,membershipId]);
 const query=useQuery({queryKey:["evaluations",organizationId,membershipId,"ADMIN",runId],queryFn:async()=>await getEvaluation(runId),enabled:Boolean(runId),retry:false,refetchInterval:q=>q.state.error?false:["QUEUED","RUNNING"].includes(q.state.data?.status??"")?2000:false});
 const run=query.isError?null:query.data??started;
 async function start(event:FormEvent){
  event.preventDefault();setError(false);
  const parsed=evaluationRequestSchema.safeParse({dataset_version_id:dataset.trim(),provider});
  if(!parsed.success){setError(true);return;}
  const fingerprint=JSON.stringify(parsed.data);
  if(attempt.current?.fingerprint!==fingerprint)attempt.current={fingerprint,key:crypto.randomUUID()};
  setBusy(true);
  try{const result=await startEvaluation(parsed.data,attempt.current.key);setStarted(result);setRunId(result.id);setOpenId(result.id);attempt.current=null;}catch{setError(true);}finally{setBusy(false);}
 }
 function open(event:FormEvent){event.preventDefault();if(!/^[0-9a-f-]{36}$/i.test(openId.trim())){setError(true);return;}setStarted(null);if(openId.trim()===runId)void query.refetch();else setRunId(openId.trim());setError(false);}
 return <section className="evaluation-page"><h2>{t("title")}</h2><p>{t("description")}</p>
 <form onSubmit={e=>void start(e)}><label>{t("datasetId")}<input value={dataset} onChange={e=>setDataset(e.target.value)} required/></label>
 <label>{t("provider")}<select value={provider} onChange={e=>setProvider(e.target.value as "mock"|"hosted")}><option value="mock">{t("mock")}</option><option value="hosted">{t("hosted")}</option></select></label>
 {provider==="hosted"&&<p>{t("hostedPolicy")}</p>}<button className="primary-button" disabled={busy} type="submit">{t("start")}</button></form>
 <form onSubmit={open}><label>{t("runId")}<input value={openId} onChange={e=>setOpenId(e.target.value)} required/></label><button className="secondary-button" type="submit">{t("open")}</button></form>
 {(error||query.isError)&&<p role="alert">{t("error")}</p>}
 {run&&<div aria-live="polite"><h3>{t(`statuses.${run.status}`)}</h3><dl><dt>{t("runId")}</dt><dd>{run.id}</dd><dt>{t("version")}</dt><dd>{run.dataset_version}</dd><dt>{t("hash")}</dt><dd>{run.dataset_hash}</dd><dt>{t("policy")}</dt><dd>{run.dataset_policy_version} / {run.provider_policy_version}</dd></dl>
 {run.failure_kind&&<p>{t(`failures.${run.failure_kind}`)}</p>}
 {run.safe_error_code&&<p>{run.safe_error_code}</p>}
 {run.result&&<><p>{t("cases")}: <span>{run.result.passed} / {run.result.total}</span></p><p>{t("failed")}: {run.result.failed}; {t("skipped")}: {run.result.skipped}</p><p>{t("hostedQuality")}: {run.result.hosted_quality}</p><p>{t("coverage")}: {run.result.gate.missing_coverage.join(", ")||t("complete")}</p><p>{t("limitations")}: {run.result.limitations.join(", ")}</p></>}
 </div>}</section>;
}
