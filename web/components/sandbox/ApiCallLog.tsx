"use client";

import { useEffect, useMemo, useState } from "react";
import type { ApiCall, Scenario } from "@/types";
import { formatDuration, relativeTime, truncate } from "@/lib/format";
import { scenarioLabel } from "@/lib/lens";

/** Calls whose effect a chat window cannot show at all — the tool's own list
 * of "the reason this log exists" (see `docs/SANDBOX.md`'s own table). Every
 * one gets the eye-slash marker regardless of colour treatment. */
const INVISIBLE_METHODS = new Set([
  "deleteMessage",
  "restrictChatMember",
  "banChatMember",
  "answerCallbackQuery",
]);

/** The subset of invisible calls that are also destructive to a member's
 * standing — these additionally get the red treatment `INVISIBLE_METHODS`
 * alone doesn't imply (`answerCallbackQuery` is invisible but harmless). */
const DESTRUCTIVE_METHODS = new Set(["banChatMember", "restrictChatMember", "deleteMessage", "kickChatMember"]);

type Kind = "destructive" | "send" | "neutral";

function classify(method: string): Kind {
  if (DESTRUCTIVE_METHODS.has(method)) return "destructive";
  if (method.startsWith("send")) return "send";
  return "neutral";
}

const KIND_STYLES: Record<Kind, string> = {
  destructive: "border-tg-red bg-tg-red/10",
  send: "border-tg-green bg-tg-green/10",
  neutral: "border-tg-divider bg-tg-hover/40",
};

const KIND_METHOD_COLOR: Record<Kind, string> = {
  destructive: "text-tg-red",
  send: "text-tg-green",
  neutral: "text-tg-text",
};

function describe(call: ApiCall): string {
  const { method, payload } = call;
  const parts: string[] = [];

  const chatId = payload.chat_id;
  if (typeof chatId === "number" || typeof chatId === "string") parts.push(`chat ${chatId}`);

  const userId = payload.user_id;
  if (typeof userId === "number" || typeof userId === "string") parts.push(`user ${userId}`);

  const text = payload.text ?? payload.caption;
  if (typeof text === "string" && text.length > 0) parts.push(`"${truncate(text, 48)}"`);

  if (payload.reply_markup) parts.push("+ keyboard");

  if (method === "restrictChatMember" || method === "banChatMember") {
    const until = payload.until_date;
    if (typeof until === "number" && until > 0) {
      parts.push(`until ${new Date(until * 1000).toLocaleTimeString()}`);
    } else {
      parts.push("indefinite");
    }
  }
  if (method === "deleteMessage" && typeof payload.message_id !== "undefined") {
    parts.push(`msg ${String(payload.message_id)}`);
  }

  return parts.join(" · ") || "—";
}

async function copyCall(call: ApiCall): Promise<void> {
  await navigator.clipboard.writeText(JSON.stringify(call, null, 2));
}

/** The "what did the bot actually do" panel — the validation surface. Newest
 * first, filterable by method, each row expandable to its full JSON payload.
 * A call's timestamp reads relative to the tester's own last action
 * (`+340ms`) while that action is still the freshest thing that happened, so
 * "I sent this, then the bot did those three things" is legible without
 * cross-referencing two clocks; it falls back to ordinary relative time
 * ("2m ago") once a later action supersedes it. */
export default function ApiCallLog(props: {
  calls: ApiCall[];
  lastActionAt: number | null;
  scenarios: Scenario[];
  /** True only when the scenario lens is "all" — see `MessageList`'s prop of
   * the same name for why a filtered-down view doesn't also get badges. */
  showScenarioTags: boolean;
}) {
  const { calls, lastActionAt, scenarios, showScenarioTags } = props;
  const [, setTick] = useState(0);
  const [methodFilter, setMethodFilter] = useState<string>("all");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [copiedAt, setCopiedAt] = useState<number | null>(null);

  // Relative timestamps go stale even when no new call arrives; re-render
  // periodically so "3s ago" (and the action-relative "+Nms") keep counting.
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const methods = useMemo(() => Array.from(new Set(calls.map((c) => c.method))).sort(), [calls]);

  const newestFirst = useMemo(() => {
    const indexed = calls.map((call, index) => ({ call, index }));
    const filtered = methodFilter === "all" ? indexed : indexed.filter((x) => x.call.method === methodFilter);
    return [...filtered].reverse();
  }, [calls, methodFilter]);

  if (calls.length === 0) {
    // `showScenarioTags` false means a scenario filter is narrowing this list
    // (see `SandboxSidebar`) — "no calls yet" would be actively misleading
    // when the real story is "none of them are this scenario's".
    return (
      <p className="text-xs text-tg-muted">
        {showScenarioTags
          ? "No Bot API calls yet — send a message or a command to see them here."
          : "No API calls tagged with this scenario."}
      </p>
    );
  }

  function toggle(index: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  return (
    <div className="space-y-1.5">
      <select
        value={methodFilter}
        onChange={(e) => setMethodFilter(e.target.value)}
        className="w-full rounded bg-tg-hover px-2 py-1 text-[11px]"
      >
        <option value="all">All methods ({calls.length})</option>
        {methods.map((method) => (
          <option key={method} value={method}>
            {method} ({calls.filter((c) => c.method === method).length})
          </option>
        ))}
      </select>

      <ul className="space-y-1">
        {newestFirst.map(({ call, index }) => {
          const kind = classify(call.method);
          const invisible = INVISIBLE_METHODS.has(call.method);
          const isExpanded = expanded.has(index);
          const relativeToAction =
            lastActionAt !== null && call.at >= lastActionAt
              ? `+${formatDuration(lastActionAt, call.at)}`
              : relativeTime(call.at);

          return (
            <li key={index} className={`rounded-md border-l-2 px-2 py-1 text-[11px] leading-tight ${KIND_STYLES[kind]}`}>
              <button type="button" onClick={() => toggle(index)} className="flex w-full items-baseline justify-between gap-2 text-left">
                <span className={`font-mono font-semibold ${KIND_METHOD_COLOR[kind]}`}>
                  {call.method}
                  {invisible && <span title="Not visible in the chat window — this log is the only place it shows up."> 👁‍🗨</span>}
                </span>
                <span className="shrink-0 text-tg-muted">{relativeToAction}</span>
              </button>
              <div className="flex items-center gap-1">
                <span className="min-w-0 flex-1 truncate text-tg-muted">{describe(call)}</span>
                {showScenarioTags && (
                  <span
                    className="shrink-0 rounded bg-black/20 px-1 py-[1px] text-[9px] text-tg-muted"
                    title={`From scenario: ${scenarioLabel(scenarios, call.scenario_id)}`}
                  >
                    {scenarioLabel(scenarios, call.scenario_id)}
                  </span>
                )}
              </div>

              {isExpanded && (
                <div className="mt-1 space-y-1">
                  <div className="flex justify-end">
                    <button
                      type="button"
                      onClick={() => {
                        void copyCall(call);
                        setCopiedAt(index);
                        setTimeout(() => setCopiedAt((c) => (c === index ? null : c)), 1500);
                      }}
                      className="rounded bg-tg-hover px-1.5 py-0.5 text-[10px] text-tg-muted hover:text-tg-text"
                    >
                      {copiedAt === index ? "Copied" : "Copy JSON"}
                    </button>
                  </div>
                  <pre className="max-h-48 overflow-auto rounded bg-black/30 p-1.5 text-[10px] text-tg-text/90">
                    {JSON.stringify(call.payload, null, 2)}
                  </pre>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
