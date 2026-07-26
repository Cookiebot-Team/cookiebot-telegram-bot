"use client";

// The scrollable timeline for the open chat. It merges three kinds of
// content into one ordered list, the way Telegram's own view does:
//
//   - real messages (`SandboxMessage[]`, from the snapshot)
//   - date separators, inserted wherever the calendar day changes
//   - service lines. Joins and leaves are stored messages carrying `service`
//     rather than text (Telegram models them that way, and the captcha needs
//     one to reply to), so they survive a reload. Everything else a membership
//     change can be — a promotion, an anonymity toggle, a restriction the bot
//     applied — leaves no message behind, and is reconstructed from the raw
//     `member` events streamed over SSE. That is why this component takes
//     `events` as well as `messages`.

import { useEffect, useMemo, useRef } from "react";
import type { Scenario, SandboxChat, SandboxEvent, SandboxMessage, SandboxUser } from "@/types";
import { dayKey, displayName, formatDateLabel } from "@/lib/format";
import { scenarioLabel } from "@/lib/lens";
import MessageBubble from "@/components/chat/MessageBubble";

interface MessageListProps {
  chat: SandboxChat;
  /** Already filtered to the current scenario lens by `app/page.tsx` — this
   * component only renders, it doesn't decide what belongs in the view. */
  messages: SandboxMessage[];
  events: SandboxEvent[];
  users: SandboxUser[];
  bot: SandboxUser | null;
  currentUserId: number | null;
  scenarios: Scenario[];
  /** True only when the lens is "all" — a scenario badge on every bubble is
   * exactly the noise a filtered-down view doesn't need, since every row
   * already belongs to the same one. */
  showScenarioTags: boolean;
  onReply: (message: SandboxMessage) => void;
  onPressButton: (message: SandboxMessage, data: string) => Promise<void> | void;
}

interface ServiceItem {
  kind: "service";
  key: string;
  at: number;
  text: string;
}

interface MessageItem {
  kind: "message";
  key: string;
  at: number;
  message: SandboxMessage;
}

type TimelineItem = ServiceItem | MessageItem;

function memberEventPayload(payload: Record<string, unknown>): { userId: number; action: string } | null {
  const userId = payload.user_id;
  if (typeof userId !== "number") return null;
  const action = payload.action;
  return { userId, action: typeof action === "string" ? action : "update" };
}

function serviceTextFor(action: string, name: string): string {
  switch (action) {
    case "join":
    case "joined":
      return `${name} joined the group`;
    case "leave":
    case "left":
      return `${name} left the group`;
    case "kick":
    case "kicked":
    case "banned":
      return `${name} was removed`;
    case "restrict":
    case "restricted":
      return `${name} was restricted`;
    default:
      return `${name}'s membership changed`;
  }
}

export default function MessageList({
  chat,
  messages,
  events,
  users,
  bot,
  currentUserId,
  scenarios,
  showScenarioTags,
  onReply,
  onPressButton,
}: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  const userById = useMemo(() => {
    const map = new Map<number, SandboxUser>();
    for (const user of users) map.set(user.id, user);
    if (bot) map.set(bot.id, bot);
    return map;
  }, [users, bot]);

  const messageById = useMemo(() => {
    const map = new Map<number, SandboxMessage>();
    for (const message of messages) map.set(message.message_id, message);
    return map;
  }, [messages]);

  const timeline = useMemo<TimelineItem[]>(() => {
    const items: TimelineItem[] = messages.map((message) =>
      message.service
        ? {
            kind: "service",
            key: `m-${message.message_id}`,
            at: message.date,
            text: serviceTextFor(
              // A leave with an actor is a removal, not a departure — the same
              // distinction `control_api.leave_chat` draws between Telegram's
              // "left" and "kicked" statuses.
              message.service.kind === "leave" && message.service.by_user_id != null
                ? "kicked"
                : String(message.service.kind),
              displayName(userById.get(Number(message.service.user_id))),
            ),
          }
        : { kind: "message", key: `m-${message.message_id}`, at: message.date, message },
    );

    for (const event of events) {
      if (event.kind !== "member") continue;
      const parsed = memberEventPayload(event.payload);
      // Joins and leaves already arrived as stored service messages above;
      // taking them from SSE as well would double every one of them.
      if (parsed && (parsed.action === "join" || parsed.action === "leave")) continue;
      const chatIdInPayload = event.payload.chat_id;
      if (!parsed || typeof chatIdInPayload !== "number" || chatIdInPayload !== chat.id) continue;
      const name = displayName(userById.get(parsed.userId));
      items.push({
        kind: "service",
        key: `e-${event.at}-${parsed.userId}-${parsed.action}`,
        at: event.at,
        text: serviceTextFor(parsed.action, name),
      });
    }

    items.sort((a, b) => a.at - b.at);
    return items;
  }, [messages, events, chat.id, userById]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [timeline.length]);

  return (
    <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto py-3">
      {timeline.length === 0 && (
        <div className="px-4 py-10 text-center text-[13px] text-tg-muted">
          {showScenarioTags ? (
            <>
              No messages yet — try sending <span className="font-mono">/rules</span> below.
            </>
          ) : (
            "No messages in this chat match the current scenario filter."
          )}
        </div>
      )}

      {timeline.map((item, index) => {
        const previous = timeline[index - 1];
        const showDateSeparator = !previous || dayKey(previous.at) !== dayKey(item.at);

        return (
          <div key={item.key}>
            {showDateSeparator && (
              <div className="my-2 flex justify-center">
                <span className="rounded-full bg-tg-panel px-3 py-1 text-[12px] text-tg-muted shadow-sm">
                  {formatDateLabel(item.at)}
                </span>
              </div>
            )}

            {item.kind === "service" ? (
              <div className="my-1 flex justify-center px-4">
                <span className="text-center text-[12px] text-tg-muted">{item.text}</span>
              </div>
            ) : (
              (() => {
                const message = item.message;
                const sender = userById.get(message.from_id);
                const senderChat = message.sender_chat_id === chat.id ? chat : undefined;
                const isBot = bot !== null && message.from_id === bot.id;
                const outgoing = currentUserId !== null && message.from_id === currentUserId && !senderChat;
                const previousMessage = previous?.kind === "message" ? previous.message : null;
                const sameSenderAsPrevious =
                  previousMessage !== null &&
                  previousMessage.from_id === message.from_id &&
                  previousMessage.sender_chat_id === message.sender_chat_id;
                const showSender = chat.type !== "private" && !showDateSeparator && !sameSenderAsPrevious;
                const repliedMessage =
                  message.reply_to_message_id !== null ? (messageById.get(message.reply_to_message_id) ?? null) : null;

                return (
                  <MessageBubble
                    message={message}
                    sender={sender}
                    senderChat={senderChat}
                    isBot={isBot}
                    outgoing={outgoing}
                    showSender={showSender || (chat.type !== "private" && showDateSeparator)}
                    repliedMessage={repliedMessage}
                    repliedSender={repliedMessage ? userById.get(repliedMessage.from_id) : undefined}
                    scenarioTag={showScenarioTags ? scenarioLabel(scenarios, message.scenario_id) : null}
                    onReply={() => onReply(message)}
                    onPressButton={(data) => onPressButton(message, data)}
                  />
                );
              })()
            )}
          </div>
        );
      })}
    </div>
  );
}
