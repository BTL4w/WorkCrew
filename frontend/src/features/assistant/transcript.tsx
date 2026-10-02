import { DailyUpdateBlock } from "./blocks/daily-update-block";
import { useTranslations } from "next-intl";

import type { ProposalContent } from "@/features/ai-proposals/contracts";
import type { CandidateOverrideInput, RecommendationVersion } from "@/features/project-team/contracts";

import type { AssistantMessage, AssistantBlock } from "./contracts";
import { ActivityBlock } from "./blocks/activity-block";
import { AssignmentResultBlock } from "./blocks/assignment-result-block";
import { PlanningBlock } from "./blocks/planning-block";
import { SafeErrorBlock } from "./blocks/safe-error-block";
import { TeamDecisionBlock } from "./blocks/team-decision-block";
import { TeamRecommendationBlock } from "./blocks/team-recommendation-block";
import { UnavailableBlock } from "./blocks/unavailable-block";
import { WorkEvidenceBlock } from "./blocks/work-evidence-block";

type ProposalBlock = Extract<AssistantBlock, { kind: "proposal" }>;
type ActivityBlockData = Extract<AssistantBlock, { kind: "activity" }>;
type TeamRecommendationBlockData = Extract<AssistantBlock, { kind: "team_recommendation" }>;
type TranscriptEntry =
  | { kind: "message"; message: AssistantMessage }
  | { kind: "activity_group"; id: string; blocks: ActivityBlockData[] };

export function Transcript({ messages, canManage, onEdit, onRevise, onApprove, onReject, onTeamRevise, onTeamManualRevise, onTeamDecide, onContinueManually }: {
  messages: AssistantMessage[];
  canManage: boolean;
  onEdit: (block: ProposalBlock, content: ProposalContent) => Promise<boolean>;
  onRevise: (block: ProposalBlock, instruction: string) => void;
  onApprove: (block: ProposalBlock) => void;
  onReject: (block: ProposalBlock) => void;
  onTeamRevise?: (block: TeamRecommendationBlockData) => void;
  onTeamManualRevise?: (recommendation: RecommendationVersion, overrides: CandidateOverrideInput[]) => Promise<RecommendationVersion | null>;
  onTeamDecide?: (recommendation: RecommendationVersion, action: "approve" | "reject", reason: string | null) => Promise<RecommendationVersion | null>;
  onContinueManually?: () => void;
}) {
  const t = useTranslations("assistant");
  const ordered = messages.toSorted((left, right) => left.sequence - right.sequence);
  const visibleMessages = collapseProposalBlocks(ordered);
  const entries = groupConsecutiveActivities(visibleMessages);
  const latestTeamVersions = latestRecommendationVersions(visibleMessages);
  return <div className="assistant-transcript" aria-live="polite">
    {visibleMessages.length === 0 ? <div className="assistant-empty"><h2>{t("empty.title")}</h2><p>{t("empty.description")}</p></div> : null}
    {entries.map((entry) => entry.kind === "activity_group" ? <article className="assistant-message role-assistant" data-side="left" key={entry.id}>
      <p className="assistant-message-role">{t("role.assistant")}</p>
      <div className="assistant-message-content">
        <ActivityBlock block={entry.blocks[0]} groupedBlocks={entry.blocks} />
      </div>
    </article> : <article className={`assistant-message role-${entry.message.role.toLowerCase()}`} data-side={entry.message.role === "USER" ? "right" : "left"} key={entry.message.id}>
      <p className="assistant-message-role">{entry.message.role === "USER" ? t("role.you") : t("role.assistant")}</p>
      <div className="assistant-message-content">{entry.message.content_blocks.map((block, index) => <BlockView
        key={`${entry.message.id}-${index}`}
        block={block}
        canManage={canManage}
        onEdit={onEdit}
        onRevise={onRevise}
        onApprove={onApprove}
        onReject={onReject}
        onTeamRevise={onTeamRevise}
        onTeamManualRevise={onTeamManualRevise}
        onTeamDecide={onTeamDecide}
        latestTeamVersions={latestTeamVersions}
        onContinueManually={onContinueManually}
      />)}</div>
    </article>)}
  </div>;
}

function latestRecommendationVersions(messages: AssistantMessage[]) {
  const versions = new Map<string, number>();
  for (const message of messages) for (const block of message.content_blocks) {
    if (block.kind === "team_recommendation") {
      versions.set(block.recommendation_id, Math.max(versions.get(block.recommendation_id) ?? 0, block.recommendation_version));
    }
  }
  return versions;
}

function collapseProposalBlocks(messages: AssistantMessage[]): AssistantMessage[] {
  const originalLocations = new Map<string, string>();
  const latestBlocks = new Map<string, ProposalBlock>();
  for (const message of messages) {
    message.content_blocks.forEach((block, index) => {
      if (block.kind === "proposal") {
        const versionKey = `${block.proposal_id}:${block.proposal_version}`;
        if (!originalLocations.has(versionKey)) {
          originalLocations.set(versionKey, `${message.id}:${index}`);
        }
        latestBlocks.set(versionKey, block);
      }
    });
  }

  return messages.flatMap((message) => {
    const contentBlocks: AssistantBlock[] = [];
    message.content_blocks.forEach((block, index) => {
      if (block.kind !== "proposal") {
        contentBlocks.push(block);
        return;
      }
      const versionKey = `${block.proposal_id}:${block.proposal_version}`;
      const isOriginalLocation = originalLocations.get(versionKey) === `${message.id}:${index}`;
      if (isOriginalLocation) contentBlocks.push(latestBlocks.get(versionKey) ?? block);
    });
    return contentBlocks.length > 0 ? [{ ...message, content_blocks: contentBlocks }] : [];
  });
}

function groupConsecutiveActivities(messages: AssistantMessage[]): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [];
  const workflowGroups = new Map<string, Extract<TranscriptEntry, { kind: "activity_group" }>>();

  for (const message of messages) {
    const blocks = activityBlocksFrom(message);
    if (blocks === null) {
      entries.push({ kind: "message", message });
      continue;
    }

    const workflowRunId = sharedWorkflowRunId(blocks);
    const workflowGroup = workflowRunId ? workflowGroups.get(workflowRunId) : undefined;
    if (workflowGroup) {
      workflowGroup.blocks.push(...blocks);
      continue;
    }

    const previous = entries.at(-1);
    if (!workflowRunId && previous?.kind === "activity_group") {
      previous.blocks.push(...blocks);
      continue;
    }

    const group: Extract<TranscriptEntry, { kind: "activity_group" }> = {
      kind: "activity_group",
      id: message.id,
      blocks: [...blocks],
    };
    entries.push(group);
    if (workflowRunId) workflowGroups.set(workflowRunId, group);
  }

  return entries;
}

function activityBlocksFrom(message: AssistantMessage): ActivityBlockData[] | null {
  if (message.role !== "ASSISTANT" || message.content_blocks.length === 0) return null;
  if (!message.content_blocks.every((block) => block.kind === "activity")) return null;
  return message.content_blocks as ActivityBlockData[];
}

function sharedWorkflowRunId(blocks: ActivityBlockData[]): string | null {
  const workflowRunIds = new Set(blocks.flatMap((block) =>
    block.workflow_run_id ? [block.workflow_run_id] : []));
  return workflowRunIds.size === 1 && blocks.every((block) => block.workflow_run_id)
    ? [...workflowRunIds][0]
    : null;
}

function BlockView({ block, canManage, onEdit, onRevise, onApprove, onReject, onTeamRevise, onTeamManualRevise, onTeamDecide, latestTeamVersions, onContinueManually }: {
  block: AssistantBlock;
  canManage: boolean;
  onEdit: (block: ProposalBlock, content: ProposalContent) => Promise<boolean>;
  onRevise: (block: ProposalBlock, instruction: string) => void;
  onApprove: (block: ProposalBlock) => void;
  onReject: (block: ProposalBlock) => void;
  onTeamRevise?: (block: TeamRecommendationBlockData) => void;
  onTeamManualRevise?: (recommendation: RecommendationVersion, overrides: CandidateOverrideInput[]) => Promise<RecommendationVersion | null>;
  onTeamDecide?: (recommendation: RecommendationVersion, action: "approve" | "reject", reason: string | null) => Promise<RecommendationVersion | null>;
  latestTeamVersions: Map<string, number>;
  onContinueManually?: () => void;
}) {
  const t = useTranslations("assistant");
  switch (block.kind) {
    case "text": return <p>{block.text}</p>;
    case "activity": return <ActivityBlock block={block} />;
    case "work_evidence": return <WorkEvidenceBlock block={block} />;
    case "question": return <section className="assistant-block assistant-question"><h3>{t("question.title")}</h3><p>{block.question}</p></section>;
    case "capability_unavailable": return <UnavailableBlock block={block} />;
    case "planning_run": return <section className="assistant-block assistant-activity"><p>{t("planning.status", { status: block.status })}</p></section>;
    case "proposal": return <PlanningBlock block={block} canManage={canManage} onEdit={onEdit} onRevise={onRevise} onApprove={onApprove} onReject={onReject} />;
    case "decision_result": return <section className="assistant-block assistant-decision" role="status"><h3>{t("decision.title")}</h3><p>{t(`decision.${block.decision}`)} · v{block.proposal_version}</p></section>;
    case "team_recommendation": return <TeamRecommendationBlock
      block={block}
      canManage={canManage}
      isLatest={latestTeamVersions.get(block.recommendation_id) === block.recommendation_version}
      onRequestRevision={onTeamRevise ?? (() => undefined)}
      onManualRevise={onTeamManualRevise ?? (async () => null)}
      onDecide={onTeamDecide ?? (async () => null)}
      onOpenTeam={onContinueManually ? () => onContinueManually() : undefined}
    />;
    case "team_decision_result": return <TeamDecisionBlock block={block} />;
    case "daily_update": return <DailyUpdateBlock block={block} />;
    case "assignment_result": return <AssignmentResultBlock block={block} />;
    case "safe_error": return <SafeErrorBlock block={block} onContinueManually={onContinueManually} />;
  }
}
