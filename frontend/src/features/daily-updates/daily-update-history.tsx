"use client";
import { useTranslations } from "next-intl";
import type { Observation } from "./reporting-contracts";
export function DailyUpdateHistory({observations,onCorrect,disabled}:{observations:Observation[];onCorrect:(observation:Observation)=>void;disabled:boolean}){
 const t=useTranslations("dailyUpdate");
 const superseded=new Set(observations.map(o=>o.item.corrects_observation_id));
 return <section className="mt-6"><h3 className="font-semibold">{t("history")}</h3>{!observations.length&&<p>{t("noHistory")}</p>}
 <ul className="space-y-3">{observations.map(o=><li key={o.id} className="rounded border p-3">
 <p>{o.item.reporting_date} · {o.item.reported_percent}% · {t("spent")}: {o.item.spent_hours??t("unknown")}</p>
 <p>{o.item.done_text}</p><p>{o.item.next_steps}</p>
 <p>{t("confirmedAt")}: {o.confirmed_at} · {o.reporting_timezone}</p>
 {o.late&&<p>{t("late")}</p>}{o.item.correction_reason&&<p>{t("reason")}: {o.item.correction_reason}</p>}
 {superseded.has(o.id)?<p>{t("superseded")}</p>:<button className="secondary-button" type="button" disabled={disabled} onClick={()=>onCorrect(o)}>{t("correct")}</button>}
 </li>)}</ul></section>;
}
