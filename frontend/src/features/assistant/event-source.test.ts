import { afterEach, describe, expect, it, vi } from "vitest";

import { connectAssistantEvents } from "./event-source";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly listeners = new Map<string, Array<(event: MessageEvent) => void>>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) { FakeEventSource.instances.push(this); }
  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener as (event: MessageEvent) => void);
    this.listeners.set(type, listeners);
  }
  emit(type: string, sequence: number) {
    const event = { lastEventId: String(sequence), data: "{}" } as MessageEvent;
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
  close() { this.closed = true; }
}

describe("Assistant EventSource", () => {
  afterEach(() => {
    FakeEventSource.instances = [];
    vi.useRealTimers();
  });

  it("invalidates REST only for sequence advances after the initial cursor", () => {
    const onSequence = vi.fn();
    const connection = connectAssistantEvents({
      conversationId: "11111111-1111-4111-8111-111111111111",
      initialSequence: 4,
      eventSourceFactory: (url) => new FakeEventSource(url) as unknown as EventSource,
      onSequence,
    });
    const source = FakeEventSource.instances[0];

    expect(source.url).toBe("/api/v1/ai/conversations/11111111-1111-4111-8111-111111111111/events");
    source.emit("assistant.turn.response.v1", 4);
    source.emit("assistant.workflow.projected.v1", 5);
    source.emit("assistant.workflow.projected.v1", 5);
    source.emit("assistant.turn.response.v1", 6);
    source.emit("assistant.conversation.titled.v1", 7);

    expect(onSequence.mock.calls.map(([sequence]) => sequence)).toEqual([5, 6, 7]);
    connection.close();
    expect(source.closed).toBe(true);
  });

  it("uses bounded REST polling while SSE reconnects", () => {
    vi.useFakeTimers();
    const onPoll = vi.fn();
    const onStatus = vi.fn();
    const connection = connectAssistantEvents({
      conversationId: "11111111-1111-4111-8111-111111111111",
      initialSequence: 0,
      eventSourceFactory: (url) => new FakeEventSource(url) as unknown as EventSource,
      onSequence: vi.fn(),
      onPoll,
      onStatus,
      pollIntervalMs: 1000,
      maxPollAttempts: 3,
    });
    const source = FakeEventSource.instances[0];

    source.onerror?.();
    vi.advanceTimersByTime(5000);
    expect(onStatus).toHaveBeenCalledWith("reconnecting");
    expect(onPoll).toHaveBeenCalledTimes(3);

    source.onopen?.();
    expect(onStatus).toHaveBeenCalledWith("connected");
    connection.close();
  });

  it("reconciles REST once when SSE opens after a fast worker response", () => {
    vi.useFakeTimers();
    const onPoll = vi.fn();
    connectAssistantEvents({
      conversationId: "11111111-1111-4111-8111-111111111111",
      initialSequence: 1,
      eventSourceFactory: (url) => new FakeEventSource(url) as unknown as EventSource,
      onSequence: vi.fn(),
      onPoll,
      pollIntervalMs: 1000,
      maxPollAttempts: 3,
    });

    const source = FakeEventSource.instances[0];
    source.onopen?.();
    vi.advanceTimersByTime(3000);

    expect(onPoll).toHaveBeenCalledTimes(3);

    source.emit("assistant.turn.response.v1", 2);
    vi.advanceTimersByTime(3000);

    expect(onPoll).toHaveBeenCalledTimes(3);
  });
});
