"use client";

// Holds the sandbox snapshot and keeps it live over SSE.
//
// The server tells us *that* something happened via `GET /api/events`; it
// does not stream the new state itself, so every event triggers a fresh
// `GET /api/state`. This trades a little bandwidth for a client that can
// never drift from the server's idea of the world — the whole point of a
// validation tool is that what you see is what actually happened.
//
// The event *payloads* are also buffered (capped, newest last) so callers can
// render things the snapshot alone can't express — a join/leave as a
// service-message in the timeline, a callback-answer toast — without the
// snapshot needing to carry history it has no reason to keep.

import { useCallback, useEffect, useRef, useState } from "react";
import type { SandboxEvent, SandboxKit, SandboxSnapshot } from "@/types";
import { getKit, getState } from "@/lib/api";

const EVENT_BUFFER_LIMIT = 200;
const RECONNECT_DELAY_MS = 1500;

// `control_api.py`'s `stream_events` only emits an SSE frame for the call
// sites that already call `sandbox.publish(...)` (a send, a membership
// change, a reset). `telegram_api.py` records *every* Bot API call the bot
// makes (`record_api_call`, the API-call log's data source) but several
// handlers with no user-visible state to publish — `answerCallbackQuery`,
// `getChatAdministrators`, `setMyCommands` — never fire an event. A poll
// underneath the SSE stream is the only way the log (and the "did the bot
// answer yet" indicator built on it) stays current for those; short enough
// to feel live, long enough not to matter at sandbox scale.
const POLL_INTERVAL_MS = 1200;

export interface UseSandboxResult {
  snapshot: SandboxSnapshot | null;
  /** What the bot under test *is* — identity, seeds, presets, commands,
   * features. Fetched once: it comes from a config file the server read at
   * startup, so it cannot change without a restart, and re-fetching it on
   * every poll would be a request that can never return anything new. */
  kit: SandboxKit | null;
  events: SandboxEvent[];
  connected: boolean;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useSandbox(): UseSandboxResult {
  const [snapshot, setSnapshot] = useState<SandboxSnapshot | null>(null);
  const [kit, setKit] = useState<SandboxKit | null>(null);
  const [events, setEvents] = useState<SandboxEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const next = await getState();
      if (!mountedRef.current) return;
      setSnapshot(next);
      setError(null);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err instanceof Error ? err.message : "Failed to load sandbox state");
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void refresh();
    // Deliberately not surfaced through `error`: a failed kit fetch leaves
    // the workbench usable (the world still loads, the bot still answers) and
    // only costs the palette and the preset buttons. Blocking the whole pane
    // on it would turn a missing config file into "the sandbox is down".
    void getKit()
      .then((next) => {
        if (mountedRef.current) setKit(next);
      })
      .catch(() => {});

    let source: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const clearReconnectTimer = () => {
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };

    const connect = () => {
      if (cancelled) return;

      const es = new EventSource("/api/events");
      source = es;

      es.onopen = () => {
        if (!cancelled) setConnected(true);
      };

      es.onmessage = (message: MessageEvent<string>) => {
        if (cancelled) return;
        try {
          const parsed = JSON.parse(message.data) as SandboxEvent;
          setEvents((prev) => {
            if (parsed.kind === "reset") return [parsed];
            const withNew = [...prev, parsed];
            return withNew.length > EVENT_BUFFER_LIMIT
              ? withNew.slice(withNew.length - EVENT_BUFFER_LIMIT)
              : withNew;
          });
        } catch {
          // A keepalive/comment frame with no JSON body — nothing to buffer,
          // but it's still a sign the connection is alive, so still refresh.
        }
        void refresh();
      };

      es.onerror = () => {
        if (cancelled) return;
        setConnected(false);
        es.close();
        if (source === es) source = null;
        clearReconnectTimer();
        reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS);
      };
    };

    connect();
    const pollId = setInterval(() => void refresh(), POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      mountedRef.current = false;
      clearReconnectTimer();
      clearInterval(pollId);
      source?.close();
      source = null;
    };
  }, [refresh]);

  return { snapshot, kit, events, connected, loading, error, refresh };
}
