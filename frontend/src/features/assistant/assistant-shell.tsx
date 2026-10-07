"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { decideApproval, editProposal } from "@/features/ai-proposals/api";
import type { ProposalContent } from "@/features/ai-proposals/contracts";
import { decideRecommendation, recordRecommendationFeedback, reviseRecommendation } from "@/features/project-team/api";
import type { CandidateOverrideInput, RecommendationVersion } from "@/features/project-team/contracts";
import type { MeResponse } from "@/shared/api/contracts";
import { ApiError, isDefinitiveMutationRejection } from "@/shared/api/client";

import { assistantKeys, createConversation, getConversation, listConversations, postAssistantMessage } from "./api";
import { Composer } from "./composer";
import { ConversationList, type AssistantNavigationSection } from "./conversation-list";
import type { AssistantBlock, AssistantConversation, PostMessageInput } from "./contracts";
import { connectAssistantEvents } from "./event-source";
import { Transcript } from "./transcript";

type ProposalBlock = Extract<AssistantBlock, { kind: "proposal" }>;
type TeamRecommendationBlock = Extract<AssistantBlock, { kind: "team_recommendation" }>;
type Attempt = { fingerprint: string; key: string };
type Connection = { close(): void };
type ConnectEvents = (options: Parameters<typeof connectAssistantEvents>[0]) => Connection;

function nextKey() { return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`; }
function syncConversationLocation(conversationId: string | null) {
  const url = new URL(globalThis.location.href);
  if (conversationId) url.searchParams.set("conversation", conversationId);
  else url.searchParams.delete("conversation");
  globalThis.history.replaceState(globalThis.history.state, "", `${url.pathname}${url.search}${url.hash}`);
}
function retryTransientQuery(failureCount: number, caught: Error) {
  return failureCount < 1 && (!(caught instanceof ApiError) || caught.status >= 500);
}
function useAttempt() {
  const current = useRef<Attempt | null>(null);
  return {
    key(payload: unknown) {
      const fingerprint = JSON.stringify(payload);
      if (current.current?.fingerprint !== fingerprint) current.current = { fingerprint, key: nextKey() };
      return current.current.key;
    },
    reset() { current.current = null; },
  };
}

export function AssistantShell({
  actor,
  initialConversationId = null,
  activeSection = "assistant",
  workspaceTitle,
  workspaceContent,
  onOpenAssistant,
  onContinueManually,
  onOpenProjects,
  onOpenMyTasks,
  onOpenPeopleCapacity,
  onOpenEvaluations,
  onAssignTask,
  isLoggingOut = false,
  logoutError = false,
  onLogout,
  connectEvents = connectAssistantEvents,
}: {
  actor: MeResponse;
  initialConversationId?: string | null;
  activeSection?: AssistantNavigationSection;
  workspaceTitle?: string;
  workspaceContent?: ReactNode;
  onOpenAssistant?: () => void;
  onContinueManually?: () => void;
  onOpenProjects?: () => void;
  onOpenMyTasks?: () => void;
  onOpenPeopleCapacity?: () => void;
  onOpenEvaluations?: () => void;
  onAssignTask?: () => void;
  isLoggingOut?: boolean;
  logoutError?: boolean;
  onLogout?: () => void | Promise<void>;
  connectEvents?: ConnectEvents;
}) {
  const t = useTranslations("assistant");
  const locale = useLocale() as "vi" | "en";
  const queryClient = useQueryClient();
  const organizationId = actor.membership.organization_id;
  const membershipId = actor.membership.id;
  const canManage = actor.membership.role !== "EMPLOYEE";
  const assistantActive = activeSection === "assistant";
  const [selectedId, setSelectedId] = useState<string | null>(initialConversationId);
  const [newConversation, setNewConversation] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [teamRevisionTarget, setTeamRevisionTarget] = useState<TeamRecommendationBlock | null>(null);
  const [teamFeedbackWarning, setTeamFeedbackWarning] = useState(false);
  const createAttempt = useAttempt();
  const messageAttempt = useAttempt();
  const editAttempt = useAttempt();
  const decisionAttempt = useAttempt();
  const teamMutationAttempt = useAttempt();

  const conversationsKey = assistantKeys.conversations(organizationId, membershipId);
  const conversations = useQuery({
    queryKey: conversationsKey,
    queryFn: listConversations,
    refetchInterval: (query) => query.state.data?.some((item) =>
      item.title === null && item.last_message_sequence > 0
      && Date.now() - Date.parse(item.updated_at) < 60000,
    ) ? 5000 : false,
    retry: retryTransientQuery,
    retryDelay: 100,
  });
  const activeConversationId = newConversation ? null : selectedId ?? conversations.data?.[0]?.id ?? null;
  const snapshotKey = activeConversationId ? assistantKeys.conversation(organizationId, membershipId, activeConversationId) : null;
  const snapshot = useQuery({
    queryKey: snapshotKey ?? [...assistantKeys.scope(organizationId, membershipId), "new"],
    queryFn: () => getConversation(activeConversationId as string).then((result) => result.data),
    enabled: assistantActive && activeConversationId !== null,
    retry: retryTransientQuery,
    retryDelay: 100,
  });
  const projectedSequence = snapshot.data?.conversation.last_event_sequence ?? 0;
  useEffect(() => {
    const current = snapshot.data?.conversation;
    if (!current) return;
    queryClient.setQueryData<AssistantConversation[]>(assistantKeys.conversations(organizationId, membershipId), (items) =>
      items?.map((item) => item.id === current.id && current.last_event_sequence >= item.last_event_sequence
        ? { ...item, ...current } : item),
    );
  }, [snapshot.data?.conversation, queryClient, organizationId, membershipId]);
  const projectedSequenceRef = useRef(projectedSequence);
  useEffect(() => {
    projectedSequenceRef.current = projectedSequence;
  }, [projectedSequence]);
  useEffect(() => {
    if (!assistantActive || !activeConversationId) return;
    const connection = connectEvents({
      conversationId: activeConversationId,
      initialSequence: projectedSequenceRef.current,
      onSequence: () => void queryClient.invalidateQueries({ queryKey: assistantKeys.conversation(organizationId, membershipId, activeConversationId) }),
      onPoll: () => void queryClient.invalidateQueries({ queryKey: assistantKeys.conversation(organizationId, membershipId, activeConversationId) }),
    });
    return () => connection.close();
  }, [activeConversationId, assistantActive, connectEvents, membershipId, organizationId, queryClient]);

  const orderedMessages = snapshot.data?.messages.toSorted((left, right) => right.sequence - left.sequence) ?? [];
  const latestUserSequence = orderedMessages.find((item) => item.role === "USER")?.sequence ?? 0;
  const questionMessage = orderedMessages.find((item) =>
    item.role === "ASSISTANT"
    && item.sequence > latestUserSequence
    && item.content_blocks.some((block) => block.kind === "question"),
  );
  const latestQuestion = questionMessage?.content_blocks.find(
    (block): block is Extract<AssistantBlock, { kind: "question" }> => block.kind === "question",
  );

  async function send(input: PostMessageInput, version?: number) {
    if (submitting) return false;
    setSubmitting(true); setError(null);
    try {
      let conversationId = activeConversationId;
      if (!conversationId) {
        const created = await createConversation({ locale, title: null }, createAttempt.key({ locale, title: null }));
        createAttempt.reset();
        conversationId = created.data.id;
        setSelectedId(conversationId); setNewConversation(false);
        syncConversationLocation(conversationId);
        await queryClient.invalidateQueries({ queryKey: conversationsKey });
      }
      await postAssistantMessage(conversationId, input, messageAttempt.key({ conversationId, input, version }), version);
      messageAttempt.reset(); setMessage("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: assistantKeys.conversation(organizationId, membershipId, conversationId) }),
        queryClient.invalidateQueries({ queryKey: conversationsKey }),
      ]);
      return true;
    } catch (caught) {
      setError(caught);
      if (isDefinitiveMutationRejection(caught)) messageAttempt.reset();
      if (caught instanceof ApiError && caught.code === "RESOURCE_VERSION_MISMATCH") await snapshot.refetch();
      return false;
    } finally { setSubmitting(false); }
  }

  function submitComposer() {
    const normalized = message.trim();
    if (!normalized) return;
    if (teamRevisionTarget) {
      const target = teamRevisionTarget;
      void send({ message: normalized, locale, card_action: {
        kind: "TEAM_REVISE",
        recommendation_id: target.recommendation_id,
        recommendation_version: target.recommendation_version,
      } }, target.recommendation_version).then((sent) => { if (sent) setTeamRevisionTarget(null); });
      return;
    }
    const workflowRunId = latestQuestion?.response_context.workflow_run_id;
    const cardAction = typeof workflowRunId === "string" ? { kind: "PLANNING_INPUT" as const, workflow_run_id: workflowRunId } : undefined;
    void send({ message: normalized, locale, ...(cardAction ? { card_action: cardAction } : {}) });
  }

  async function saveEdit(block: ProposalBlock, content: ProposalContent): Promise<boolean> {
    if (submitting) return false;
    const payload = { proposalId: block.proposal_id, version: block.proposal_version, content };
    setSubmitting(true); setError(null);
    try {
      await editProposal(block.proposal_id, content, block.proposal_version, editAttempt.key(payload));
      editAttempt.reset(); await snapshot.refetch();
      return true;
    } catch (caught) {
      setError(caught); if (isDefinitiveMutationRejection(caught)) editAttempt.reset();
      if (caught instanceof ApiError && ["RESOURCE_VERSION_MISMATCH", "PROPOSAL_STALE"].includes(caught.code)) await snapshot.refetch();
      return false;
    } finally { setSubmitting(false); }
  }
  function revise(block: ProposalBlock, instruction: string) {
    void send({ message: instruction, locale, card_action: { kind: "PLANNING_REVISE", workflow_run_id: block.workflow_run_id, proposal_id: block.proposal_id } }, block.proposal_version);
  }
  async function decide(block: ProposalBlock, decision: "APPROVE" | "REJECT") {
    if (!block.approval_id || submitting) return;
    const payload = { approvalId: block.approval_id, decision, version: block.proposal_version };
    setSubmitting(true); setError(null);
    try {
      await decideApproval(block.approval_id, decision, block.proposal_version, null, decisionAttempt.key(payload));
      decisionAttempt.reset();
      if (decision === "APPROVE") {
        await queryClient.invalidateQueries({ queryKey: ["work", organizationId, membershipId] });
      }
      await snapshot.refetch();
    } catch (caught) {
      setError(caught); if (isDefinitiveMutationRejection(caught)) decisionAttempt.reset();
      if (caught instanceof ApiError && ["RESOURCE_VERSION_MISMATCH", "PROPOSAL_STALE", "APPROVAL_STATE_CONFLICT"].includes(caught.code)) await snapshot.refetch();
    } finally { setSubmitting(false); }
  }

  async function reviseTeam(base: RecommendationVersion, overrides: CandidateOverrideInput[]) {
    const payload = { recommendationId: base.recommendation_id, version: base.version, overrides };
    setSubmitting(true); setError(null); setTeamFeedbackWarning(false);
    try {
      const result = await reviseRecommendation(base.recommendation_id, overrides, base.version, teamMutationAttempt.key(payload));
      teamMutationAttempt.reset();
      const comment = overrides.map((override) => override.override_reason?.trim()).filter(Boolean).join("; ");
      try {
        await recordRecommendationFeedback(base.recommendation_id, result.data.version, "override", comment, teamMutationAttempt.key({ ...payload, action: "feedback", version: result.data.version, comment }));
        teamMutationAttempt.reset();
      } catch (caught) {
        if (isDefinitiveMutationRejection(caught)) teamMutationAttempt.reset();
        setTeamFeedbackWarning(true);
      }
      await snapshot.refetch();
      return result.data;
    } catch (caught) {
      setError(caught); if (isDefinitiveMutationRejection(caught)) teamMutationAttempt.reset();
      if (caught instanceof ApiError && caught.code === "RESOURCE_VERSION_MISMATCH") await snapshot.refetch();
      return null;
    } finally { setSubmitting(false); }
  }

  async function decideTeam(base: RecommendationVersion, action: "approve" | "reject", reason: string | null) {
    const payload = { recommendationId: base.recommendation_id, version: base.version, action, reason };
    setSubmitting(true); setError(null); setTeamFeedbackWarning(false);
    try {
      const result = await decideRecommendation(base.recommendation_id, base.version, action, reason, teamMutationAttempt.key(payload));
      teamMutationAttempt.reset();
      try {
        await recordRecommendationFeedback(base.recommendation_id, base.version, action === "approve" ? "accept" : "reject", reason ?? "", teamMutationAttempt.key({ ...payload, action: "feedback" }));
        teamMutationAttempt.reset();
      } catch (caught) {
        if (isDefinitiveMutationRejection(caught)) teamMutationAttempt.reset();
        setTeamFeedbackWarning(true);
      }
      await queryClient.invalidateQueries({ queryKey: ["projectTeam"] });
      await snapshot.refetch();
      return result.data;
    } catch (caught) {
      setError(caught); if (isDefinitiveMutationRejection(caught)) teamMutationAttempt.reset();
      if (caught instanceof ApiError && caught.code === "RESOURCE_VERSION_MISMATCH") await snapshot.refetch();
      return null;
    } finally { setSubmitting(false); }
  }

  const visibleError = error ?? snapshot.error ?? conversations.error;

  return <section className={`assistant-shell ${collapsed ? "is-sidebar-collapsed" : ""} ${assistantActive ? "" : "has-workspace-pane"}`} aria-labelledby={assistantActive ? "assistant-title" : "workspace-title"}>
    <ConversationList
      actor={actor}
      conversations={conversations.data ?? []}
      selectedId={activeConversationId}
      activeSection={activeSection}
      collapsed={collapsed}
      onSelect={(id) => { setSelectedId(id); setNewConversation(false); setError(null); syncConversationLocation(id); onOpenAssistant?.(); }}
      onNew={() => { setSelectedId(null); setNewConversation(true); setMessage(""); setError(null); syncConversationLocation(null); onOpenAssistant?.(); }}
      onToggle={() => setCollapsed((value) => !value)}
      onOpenProjects={onOpenProjects}
      onOpenMyTasks={onOpenMyTasks}
      onOpenPeopleCapacity={onOpenPeopleCapacity}
      onOpenEvaluations={onOpenEvaluations}
      onAssignTask={onAssignTask}
      isLoggingOut={isLoggingOut}
      logoutError={logoutError}
      onLogout={onLogout}
    />
    {assistantActive ? <div className="assistant-main-pane">
        <header className="assistant-header"><div><p>{t("eyebrow")}</p><h1 id="assistant-title">{t("title")}</h1></div></header>
        {visibleError ? <div className="assistant-safe-notice" role="alert"><p>{t("error.safe")}</p>{visibleError instanceof ApiError && visibleError.requestId ? <p>{t("error.reference", { requestId: visibleError.requestId })}</p> : null}</div> : null}
        {teamFeedbackWarning ? <div className="assistant-safe-notice" role="status"><p>{t("teamRecommendation.feedbackWarning")}</p></div> : null}
        {snapshot.isPending && activeConversationId ? <p role="status">{t("loading")}</p> : <Transcript
          messages={snapshot.data?.messages ?? []}
          canManage={canManage}
          onEdit={saveEdit}
          onRevise={revise}
          onApprove={(block) => void decide(block, "APPROVE")}
          onReject={(block) => void decide(block, "REJECT")}
          onTeamRevise={(block) => { setTeamRevisionTarget(block); setMessage(""); setError(null); }}
          onTeamManualRevise={reviseTeam}
          onTeamDecide={decideTeam}
          onContinueManually={onContinueManually}
        />}
        {teamRevisionTarget ? <div className="assistant-revision-context" role="status"><span>{t("teamRecommendation.revisionContext", { version: teamRevisionTarget.recommendation_version })}</span><button type="button" onClick={() => setTeamRevisionTarget(null)}>{t("teamRecommendation.cancelRevision")}</button></div> : null}
        <Composer value={message} disabled={submitting} autoFocus={!activeConversationId || (snapshot.data?.messages.length ?? 0) === 0} onChange={setMessage} onSubmit={submitComposer} />
      </div> : <div className="assistant-workspace-pane">
        <header className="assistant-workspace-header">
          <p>{actor.membership.organization_name}</p>
          <div id="workspace-title">{workspaceTitle}</div>
        </header>
        <main className="assistant-workspace-content">{workspaceContent}</main>
      </div>}
  </section>;
}
