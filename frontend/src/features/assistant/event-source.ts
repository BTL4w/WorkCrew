const EVENT_TYPES = [
  "assistant.turn.queued.v1",
  "assistant.turn.response.v1",
  "assistant.workflow.projected.v1",
  "assistant.conversation.titled.v1",
  "assistant.conversation.updated.v1",
] as const;

export type AssistantConnectionStatus = "connecting" | "connected" | "reconnecting";

export function connectAssistantEvents({
  conversationId,
  initialSequence,
  onSequence,
  onStatus = () => undefined,
  onPoll = () => undefined,
  eventSourceFactory = (url) => new EventSource(url),
  pollIntervalMs = 5000,
  maxPollAttempts = 12,
}: {
  conversationId: string;
  initialSequence: number;
  onSequence: (sequence: number) => void;
  onStatus?: (status: AssistantConnectionStatus) => void;
  onPoll?: () => void;
  eventSourceFactory?: (url: string) => EventSource;
  pollIntervalMs?: number;
  maxPollAttempts?: number;
}) {
  let lastSequence = initialSequence;
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let pollAttempts = 0;
  let closed = false;
  onStatus("connecting");
  const source = eventSourceFactory(`/api/v1/ai/conversations/${conversationId}/events`);

  function stopPolling() {
    if (pollTimer !== null) clearInterval(pollTimer);
    pollTimer = null;
  }
  function pollOnce() {
    if (pollAttempts >= maxPollAttempts) {
      stopPolling();
      return;
    }
    pollAttempts += 1;
    onPoll();
    if (pollAttempts >= maxPollAttempts) stopPolling();
  }
  function startPolling(immediate = false) {
    if (pollTimer !== null || maxPollAttempts <= 0) return;
    if (immediate) pollOnce();
    if (pollAttempts < maxPollAttempts) pollTimer = setInterval(pollOnce, pollIntervalMs);
  }
  function handleEvent(event: MessageEvent) {
    const sequence = Number.parseInt(event.lastEventId, 10);
    if (!Number.isSafeInteger(sequence) || sequence <= lastSequence) return;
    lastSequence = sequence;
    onSequence(sequence);
  }
  for (const type of EVENT_TYPES) source.addEventListener(type, handleEvent as EventListener);
  source.onopen = () => {
    if (closed) return;
    pollAttempts = 0;
    stopPolling();
    startPolling(true);
    onStatus("connected");
  };
  source.onerror = () => {
    if (closed) return;
    onStatus("reconnecting");
    startPolling();
  };

  return {
    close() {
      closed = true;
      stopPolling();
      source.close();
    },
  };
}
