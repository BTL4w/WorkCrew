import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";
import { useAuth } from "@/features/auth/auth-provider";
import { getPlanningDraft } from "../draft-api";
import type { AssistantBlock } from "../contracts";
import type { PlanningDraftSnapshot } from "../draft-contracts";

type Block = Extract<AssistantBlock, { kind: "planning_draft" }>;
export function PlanningDraftBlock({
  block,
  canManage,
  onPrepare,
}: {
  block: Block;
  canManage: boolean;
  onPrepare?: (block: Block, anchorDate: string) => void;
}) {
  const t = useTranslations("assistant.draft");
  const { actor } = useAuth();
  const [anchor, setAnchor] = useState("");
  const snapshot = useQuery({
    queryKey: [
      "work",
      actor?.membership.organization_id,
      actor?.membership.id,
      "planning-draft",
      block.draft_id,
      block.draft_version,
    ],
    queryFn: () => getPlanningDraft(block.draft_id, block.draft_version),
    enabled: canManage,
  });
  if (!canManage) return null;
  if (snapshot.isError) return <p role="alert">{t("unavailable")}</p>;
  if (!snapshot.data) return <p role="status">{t("loading")}</p>;
  return (
    <DraftView
      snapshot={snapshot.data}
      block={block}
      anchor={anchor || snapshot.data.suggested_anchor_date || ""}
      setAnchor={setAnchor}
      onPrepare={onPrepare}
    />
  );
}
function DraftView({
  snapshot,
  block,
  anchor,
  setAnchor,
  onPrepare,
}: {
  snapshot: PlanningDraftSnapshot;
  block: Block;
  anchor: string;
  setAnchor: (value: string) => void;
  onPrepare?: (block: Block, anchorDate: string) => void;
}) {
  const t = useTranslations("assistant.draft");
  const data = snapshot.content;
  const current = snapshot.current_version === block.draft_version;
  const ready =
    snapshot.can_prepare &&
    !snapshot.validation.errors.length &&
    !snapshot.validation.conflicts.length;
  const assumptions = [
    data.brief.audience,
    data.brief.timing,
    ...data.assumptions,
  ].filter((field) => field.status === "AI_ASSUMPTION");
  return (
    <section
      className="assistant-block assistant-planning-draft"
      aria-label={t("title")}
      data-draft-id={snapshot.draft_id}
      data-draft-version={snapshot.draft_version}
    >
      <header>
        <p>
          {t("label")} · v{snapshot.draft_version}
          {!current ? ` · ${t("historical")}` : ""}
        </p>
        <h3>{String(data.brief.objective.value)}</h3>
      </header>
      <dl>
        <dt>{t("audience")}</dt>
        <dd>{String(data.brief.audience.value ?? t("unknown"))}</dd>
        <dt>{t("duration")}</dt>
        <dd>{t("days", { count: data.duration_days })}</dd>
        {data.brief.budget ? (
          <>
            <dt>{t("budget")}</dt>
            <dd>
              {data.brief.budget.amount?.toLocaleString()}{" "}
              {data.brief.budget.currency} · {t("constraint")}
              {data.brief.budget.status === "AI_ASSUMPTION" ? (
                <small>
                  {" "}
                  · {t("aiAssumption")} · {data.brief.budget.assumption_reason}
                </small>
              ) : null}
            </dd>
          </>
        ) : null}
      </dl>
      <ol>
        {data.phases.map((phase) => (
          <li key={phase.ref}>
            <strong>{phase.title}</strong> ·{" "}
            {t("weeks", {
              start: Math.floor(phase.start_day / 7) + 1,
              end: Math.ceil(phase.end_day / 7),
            })}
          </li>
        ))}
      </ol>
      <h4>{t("assumptions")}</h4>
      <ul>
        {assumptions.map((field, index) => (
          <li key={index}>
            {String(field.value)}{" "}
            <small>
              · {t("aiAssumption")} · {field.assumption_reason}
            </small>
          </li>
        ))}
      </ul>
      {snapshot.validation.conflicts.map((conflict) => (
        <p role="alert" key={conflict.code}>
          {t("conflict", {
            requested: conflict.requested_days,
            minimum: conflict.minimum_days,
          })}{" "}
          {conflict.chain_refs.join(" → ")}{" "}
          {conflict.estimated ? t("underEstimates") : null}
        </p>
      ))}
      {snapshot.change_summary ? <p>{snapshot.change_summary}</p> : null}
      <details>
        <summary>{t("details")}</summary>
        <ul>
          {data.tasks.map((task) => (
            <li key={task.ref}>
              {task.title} ·{" "}
              {t("dayRange", { start: task.start_day + 1, end: task.end_day })}
            </li>
          ))}
        </ul>
      </details>
      {snapshot.question ? <p>{snapshot.question.text}</p> : null}
      {current && ready && onPrepare ? (
        <div>
          <label>
            {t("anchor")}
            <input
              type="date"
              value={anchor}
              onChange={(event) => setAnchor(event.target.value)}
            />
          </label>
          {snapshot.suggested_anchor_date ? (
            <p>{t("anchorSuggestion")}</p>
          ) : null}
          <p>{t("prepareNotice")}</p>
          <button
            type="button"
            disabled={!anchor}
            onClick={() => onPrepare(block, anchor)}
          >
            {t("prepare")}
          </button>
        </div>
      ) : null}
    </section>
  );
}
