import { useState } from "react";
import { useTranslations } from "next-intl";

import type { AssistantBlock } from "../contracts";

const retrievalStages: Record<string, string> = {
  "assistant.retrieval.searching": "retrieval.searching",
  "assistant.retrieval.reading": "retrieval.reading",
  "assistant.retrieval.summarizing": "retrieval.summarizing",
};

type Block = Extract<AssistantBlock, { kind: "activity" }>;

export function ActivityBlock({ block, groupedBlocks }: { block: Block; groupedBlocks?: Block[] }) {
  const t = useTranslations("assistant");
  const [expanded, setExpanded] = useState(false);
  const blocks = groupedBlocks ?? [block];
  const agents = [...new Set(blocks.flatMap((item) => item.agent_id ? [item.agent_id] : []))];
  const retrieval = blocks.filter(item => item.agent_id === "retrieve" && retrievalStages[item.label_key]);
  const latestStages = [...new Map(retrieval.map(item => [item.label_key, item])).values()];
  const status = aggregateStatus(latestStages.length ? latestStages : blocks);
  const label = retrieval.at(-1)?.label_key;
  const labelKey = label ? status === "COMPLETED" ? `${retrievalStages[label]}Complete` : status === "FAILED" ? "retrieval.stopped" : retrievalStages[label] : "activity.default";
  return <section className="assistant-block assistant-activity">
    <div className="assistant-activity-summary">
      <span className={`assistant-status-dot status-${status.toLowerCase()}`} aria-hidden="true" />
      <p>{t(labelKey)}</p>
      <button aria-expanded={expanded} type="button" onClick={() => setExpanded((value) => !value)}>{t("activity.details")}</button>
    </div>
    {expanded ? <dl>
      {agents.length > 0 ? <><dt>{t("activity.agent")}</dt><dd>{agents.join(", ")}</dd></> : null}
      <dt>{t("activity.step")}</dt><dd>{t("activity.safeSteps", { count: blocks.length })}</dd>
      <dt>{t("activity.status")}</dt><dd>{t(`status.${status}`)}</dd>
    </dl> : null}
  </section>;
}

function aggregateStatus(blocks: Block[]): Block["status"] {
  if (blocks.some((block) => block.status === "FAILED")) return "FAILED";
  if (blocks.some((block) => block.status === "RUNNING")) return "RUNNING";
  if (blocks.some((block) => block.status === "PENDING")) return "PENDING";
  return "COMPLETED";
}
