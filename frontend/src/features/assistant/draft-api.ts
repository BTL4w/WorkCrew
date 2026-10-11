import { requestJson } from "@/shared/api/client";
import { planningDraftSnapshotSchema } from "./draft-contracts";
export function getPlanningDraft(draftId: string, version?: number) {
  return requestJson(
    `/api/v1/planning-drafts/${draftId}${version ? `/versions/${version}` : ""}`,
    { schema: planningDraftSnapshotSchema },
  );
}
