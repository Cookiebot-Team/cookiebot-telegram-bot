"use client";

// One message, rendered the way Telegram Desktop renders one message:
// rounded bubble, tinted+right for what "I" sent, plain+left for everyone
// else, sender name in a per-user colour above group messages, a small
// meta line (time, edited flag) trailing the content. Bot messages get an
// explicit "BOT" tag — that's the one piece of chrome this whole tool exists
// to make unmissable. Deleted messages render as an obvious ghost rather than
// silently vanishing, because *seeing* a deletion happen is the feature this
// sandbox validates.

import type { ReactNode } from "react";
import type { InlineButton, MessageEntity, SandboxChat, SandboxMessage, SandboxUser } from "@/types";
import { colorForId, displayName, formatTime, truncate } from "@/lib/format";
import { renderInlineHtml } from "@/lib/sanitizeHtml";
import { renderEntities } from "@/lib/renderEntities";
import InlineKeyboard from "@/components/chat/InlineKeyboard";
import MessageMedia from "@/components/chat/MessageMedia";

// The sandbox stores what real Telegram stores: plain text plus entities. A
// message persisted before that landed still has literal `<b>`-style markup in
// its text and no entities, so both renderers stay — which one applies is
// decided per message, not globally.
function renderBody(text: string, entities: MessageEntity[] | undefined): ReactNode {
  return entities && entities.length > 0 ? renderEntities(text, entities) : renderInlineHtml(text);
}

interface MessageBubbleProps {
  message: SandboxMessage;
  sender: SandboxUser | undefined;
  senderChat: SandboxChat | undefined;
  isBot: boolean;
  outgoing: boolean;
  showSender: boolean;
  repliedMessage: SandboxMessage | null;
  repliedSender: SandboxUser | undefined;
  /** Which scenario produced this message, only when the timeline isn't
   * already filtered to one — see `MessageList`'s `showScenarioTags`. `null`
   * suppresses the badge entirely, including for untagged messages, so a
   * filtered-to-one-scenario view stays exactly as uncluttered as before this
   * feature landed. */
  scenarioTag: string | null;
  onReply: () => void;
  onPressButton: (data: string) => Promise<void> | void;
}

export default function MessageBubble({
  message,
  sender,
  senderChat,
  isBot,
  outgoing,
  showSender,
  repliedMessage,
  repliedSender,
  scenarioTag,
  onReply,
  onPressButton,
}: MessageBubbleProps) {
  const senderLabel = senderChat ? senderChat.title : displayName(sender);
  const align = outgoing ? "items-end" : "items-start";
  // A sticker sits on the background with no bubble behind it, as Telegram
  // draws one. Not decoration: a sticker inside a tinted rounded rectangle
  // reads as an image *attachment*, and anyone testing a sticker-flood rule
  // needs to tell those two apart at a glance.
  const bareSticker = message.media === "sticker" && !message.deleted && !message.text;
  const bubbleTone = bareSticker ? "bg-transparent" : outgoing ? "bg-tg-out" : "bg-tg-in";

  async function handlePress(button: InlineButton) {
    if (!button.callback_data) return;
    await onPressButton(button.callback_data);
  }

  return (
    <div className={`group flex w-full flex-col ${align} px-4 py-0.5`}>
      <div className="relative max-w-[70%]">
        <button
          type="button"
          onClick={onReply}
          title="Reply"
          className="absolute -left-7 top-1 hidden h-6 w-6 items-center justify-center rounded-full bg-tg-panel text-xs text-tg-muted hover:text-tg-accent group-hover:flex"
        >
          ↩
        </button>

        <div
          className={`rounded-lg px-3 py-2 ${bareSticker ? "" : "shadow-sm"} ${
            message.deleted ? "border border-dashed border-tg-muted/40 bg-transparent" : bubbleTone
          }`}
        >
          {showSender && !message.deleted && (
            <div className="mb-0.5 flex items-center gap-1.5 text-[13px] font-medium" style={{ color: colorForId(senderChat?.id ?? sender?.id ?? 0) }}>
              <span className="truncate">{senderLabel}</span>
              {isBot && (
                <span className="rounded bg-tg-accent/90 px-1 py-[1px] text-[10px] font-bold tracking-wide text-white">
                  BOT
                </span>
              )}
            </div>
          )}

          {message.deleted ? (
            <div className="flex items-center gap-1.5 text-sm italic text-tg-muted">
              <span>🗑</span>
              <span>Message deleted</span>
            </div>
          ) : (
            <>
              {repliedMessage && (
                <div className="mb-1 rounded border-l-2 border-tg-accent/70 bg-black/10 px-2 py-1 text-[12px] text-tg-text/80">
                  <div className="font-medium text-tg-accent">{displayName(repliedSender)}</div>
                  <div className="truncate">
                    {repliedMessage.deleted
                      ? "Message deleted"
                      : truncate(repliedMessage.text ?? repliedMessage.media_caption ?? "Media", 80)}
                  </div>
                </div>
              )}

              {message.media && (
                <div className="mb-1">
                  <MessageMedia message={message} />
                </div>
              )}

              {message.media_caption && (
                <div className="whitespace-pre-wrap text-[14px] leading-snug">
                  {renderBody(message.media_caption, message.caption_entities)}
                </div>
              )}

              {message.text && (
                <div className="whitespace-pre-wrap break-words text-[14px] leading-snug">
                  {renderBody(message.text, message.entities)}
                </div>
              )}

              {message.reply_markup && (
                <InlineKeyboard rows={message.reply_markup.inline_keyboard} onPress={handlePress} />
              )}

              <div className="mt-0.5 flex items-center justify-end gap-1 text-[11px] text-tg-muted">
                {scenarioTag && (
                  <span
                    className="mr-auto max-w-[55%] truncate rounded bg-black/20 px-1 py-[1px] text-[10px]"
                    title={`From scenario: ${scenarioTag}`}
                  >
                    {scenarioTag}
                  </span>
                )}
                {message.edited && <span className="italic">edited</span>}
                <span>{formatTime(message.date)}</span>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
