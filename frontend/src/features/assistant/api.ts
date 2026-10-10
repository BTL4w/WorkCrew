import { requestJson, requestJsonWithMetadata, type ApiResult } from "@/shared/api/client";

import {
  assistantTurnAcceptedSchema,
  conversationListSchema,
  conversationSchema,
  conversationSnapshotSchema,
  postMessageInputSchema,
  type AssistantConversation,
  type AssistantTurnAccepted,
  type ConversationSnapshot,
  type PostMessageInput,
} from "./contracts";

const jsonHeaders = { "Content-Type": "application/json" };

export const assistantKeys = {
  scope: (organizationId: string, membershipId: string) =>
    ["assistant", organizationId, membershipId] as const,
  conversations: (organizationId: string, membershipId: string) =>
    ["assistant", organizationId, membershipId, "conversations"] as const,
  conversation: (organizationId: string, membershipId: string, conversationId: string) =>
    ["assistant", organizationId, membershipId, "conversation", conversationId] as const,
};

export function createConversation(
  input: { locale: "vi" | "en"; title: string | null },
  key: string,
): Promise<ApiResult<AssistantConversation>> {
  return requestJsonWithMetadata("/api/v1/ai/conversations", {
    schema: conversationSchema,
    expectedStatus: 201,
    init: {
      method: "POST",
      headers: { ...jsonHeaders, "Idempotency-Key": key },
      body: JSON.stringify(input),
    },
  });
}

export function listConversations(): Promise<AssistantConversation[]> {
  return requestJson("/api/v1/ai/conversations", { schema: conversationListSchema })
    .then((result) => result.items);
}

export function getConversation(conversationId: string): Promise<ApiResult<ConversationSnapshot>> {
  return requestJsonWithMetadata(`/api/v1/ai/conversations/${conversationId}`, {
    schema: conversationSnapshotSchema,
  });
}

export function postAssistantMessage(
  conversationId: string,
  input: PostMessageInput,
  key: string,
  version?: number,
): Promise<ApiResult<AssistantTurnAccepted>> {
  const payload = postMessageInputSchema.parse(input);
  return requestJsonWithMetadata(`/api/v1/ai/conversations/${conversationId}/messages`, {
    schema: assistantTurnAcceptedSchema,
    expectedStatus: 202,
    init: {
      method: "POST",
      headers: {
        ...jsonHeaders,
        "Idempotency-Key": key,
        ...(version === undefined ? {} : { "If-Match": `"${version}"` }),
      },
      body: JSON.stringify(payload),
    },
  });
}


export function manageConversation(conversation: AssistantConversation, change: { title: string } | { is_pinned: boolean } | "delete", key: string) {
  return requestJsonWithMetadata(`/api/v1/ai/conversations/${conversation.id}`, {
    schema: conversationSchema,
    init: {
      method: change === "delete" ? "DELETE" : "PATCH",
      headers: { ...jsonHeaders, "Idempotency-Key": key, "If-Match": `"${conversation.version}"` },
      ...(change === "delete" ? {} : { body: JSON.stringify(change) }),
    },
  });
}
