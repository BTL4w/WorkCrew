import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslations } from "next-intl";

import { getProposalVersion } from "@/features/ai-proposals/api";
import { ProposalCard } from "@/features/ai-proposals/cards/proposal-card";
import type { ProposalContent } from "@/features/ai-proposals/contracts";
import { ProposalEditor } from "@/features/ai-proposals/proposal-editor";

import type { AssistantBlock } from "../contracts";
import { RiskPlanCard } from "./risk-plan-card";

type Block = Extract<AssistantBlock, { kind: "proposal" }>;

export function PlanningBlock({
  block,
  canManage,
  onEdit,
  onRevise,
  onApprove,
  onReject,
}: {
  block: Block;
  canManage: boolean;
  onEdit: (block: Block, content: ProposalContent) => Promise<boolean>;
  onRevise: (block: Block, instruction: string) => void;
  onApprove: (block: Block) => void;
  onReject: (block: Block) => void;
}) {
  const t = useTranslations("assistant");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [revising, setRevising] = useState(false);
  const [instruction, setInstruction] = useState("");
  const proposalVersion = useQuery({
    queryKey: [
      "assistant",
      "proposal-version",
      block.proposal_id,
      block.proposal_version,
    ],
    queryFn: () =>
      getProposalVersion(block.proposal_id, block.proposal_version).then(
        (result) => result.data,
      ),
  });
  const proposal = proposalVersion.data;
  const stale =
    block.read_only ||
    (proposal !== undefined &&
      proposal.current_version !== block.proposal_version) ||
    (block.current_version !== null &&
      block.current_version !== undefined &&
      block.current_version !== block.proposal_version);
  const validationBlocked =
    block.error_codes.length > 0 || block.can_approve === false;
  if (proposalVersion.isPending)
    return <p role="status">{t("proposal.loading")}</p>;
  if (!proposal) return <SafePlanningFallback block={block} />;
  if (proposal.content.risk_replan)
    return (
      <RiskPlanCard
        key={`${block.proposal_id}:${proposal.version}`}
        content={proposal.content}
        version={proposal.version}
        stale={stale}
        canManage={canManage}
        canApprove={!validationBlocked}
        validationReasons={block.error_codes}
        onEdit={(content) => onEdit(block, content)}
        onRevise={(instruction) => onRevise(block, instruction)}
        onApprove={() => onApprove(block)}
        onReject={() => onReject(block)}
      />
    );
  return (
    <section className={`assistant-planning-card ${stale ? "is-stale" : ""}`}>
      <div className="assistant-planning-status">
        <span aria-hidden="true" />
        <p>
          {stale ? t("proposal.status.stale") : t("proposal.status.pending")}
        </p>
      </div>
      {stale ? (
        <p className="assistant-stale-notice" role="status">
          {t("proposal.stale", {
            version: block.current_version ?? proposal.current_version,
          })}
        </p>
      ) : null}
      {editing ? (
        <ProposalEditor
          initial={proposal.content}
          saving={saving}
          onCancel={() => setEditing(false)}
          onSave={(content) => {
            setSaving(true);
            void onEdit(block, content)
              .then((saved) => {
                if (saved) setEditing(false);
              })
              .finally(() => setSaving(false));
          }}
        />
      ) : (
        <ProposalCard
          content={proposal.content}
          version={proposal.version}
          provenance={proposal.creator_type}
          editable={false}
          onEdit={() => undefined}
        />
      )}
      {!editing ? (
        <div
          className={`assistant-proposal-validation ${validationBlocked ? "is-blocked" : "is-ready"}`}
          role="status"
        >
          <span aria-hidden="true">{validationBlocked ? "!" : "✓"}</span>
          <div>
            <strong>
              {validationBlocked
                ? t("proposal.validationFailed")
                : t("proposal.validationReady")}
            </strong>
            <p>{t("proposal.noRowsBeforeApproval")}</p>
          </div>
        </div>
      ) : null}
      {canManage && !stale && !editing ? (
        <footer className="assistant-proposal-footer">
          <p>{t("proposal.approvalHint")}</p>
          <div className="assistant-proposal-actions">
            <button
              className="is-reject"
              type="button"
              onClick={() => onReject(block)}
            >
              {t("proposal.reject")}
            </button>
            <button type="button" onClick={() => setEditing(true)}>
              {t("proposal.edit")}
            </button>
            <button type="button" onClick={() => setRevising(true)}>
              {t("proposal.askAi")}
            </button>
            <button
              className="is-primary"
              disabled={block.can_approve === false}
              type="button"
              onClick={() => onApprove(block)}
            >
              {t("proposal.approve")}
            </button>
          </div>
        </footer>
      ) : null}
      {revising ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (instruction.trim()) onRevise(block, instruction.trim());
          }}
        >
          <label>
            {t("proposal.revisionLabel")}
            <textarea
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
            />
          </label>
          <button disabled={!instruction.trim()} type="submit">
            {t("proposal.sendRevision")}
          </button>
          <button type="button" onClick={() => setRevising(false)}>
            {t("proposal.cancel")}
          </button>
        </form>
      ) : null}
    </section>
  );
}

function SafePlanningFallback({ block }: { block: Block }) {
  const t = useTranslations("assistant");
  return (
    <section className="assistant-block assistant-safe-error" role="alert">
      <p>{t("proposal.unavailable")}</p>
      {block.manual_fallback ? <p>{block.manual_fallback}</p> : null}
    </section>
  );
}
