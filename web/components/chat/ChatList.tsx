"use client";

// The left pane. Two jobs: pick which sandbox user you're "logged in" as
// (there is no real per-account session here — the sandbox is one shared
// world, so this is the stand-in for "which phone am I holding"), and pick
// which chat to open. Both selections are lifted to the page so the centre
// and right panes can react to them.

import type { SandboxSnapshot } from "@/types";
import { colorForId, displayName, formatTime, initial, truncate } from "@/lib/format";

interface ChatListProps {
  snapshot: SandboxSnapshot | null;
  currentUserId: number | null;
  chatId: number | null;
  onSelectChat: (chatId: number) => void;
  onSelectUser: (userId: number) => void;
}

export default function ChatList({ snapshot, currentUserId, chatId, onSelectChat, onSelectUser }: ChatListProps) {
  const humans = snapshot?.users.filter((user) => !user.is_bot) ?? [];
  const chats = snapshot?.chats ?? [];

  return (
    <div className="flex h-full flex-col bg-tg-panel">
      <div className="shrink-0 border-b border-tg-divider px-4 py-3">
        <div className="text-[15px] font-semibold">Cookiebot Sandbox</div>
        <div className="mt-2">
          <label className="mb-1 block text-[11px] uppercase tracking-wide text-tg-muted">Acting as</label>
          {humans.length === 0 ? (
            <div className="text-[13px] text-tg-muted">No users yet — create one from the sidebar.</div>
          ) : (
            <select
              value={currentUserId ?? ""}
              onChange={(event) => onSelectUser(Number(event.target.value))}
              className="w-full rounded-md border border-tg-divider bg-tg-in px-2 py-1.5 text-[13px] text-tg-text focus:border-tg-accent focus:outline-none"
            >
              {humans.map((user) => (
                <option key={user.id} value={user.id}>
                  {displayName(user)} (@{user.username})
                </option>
              ))}
            </select>
          )}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {chats.length === 0 ? (
          <div className="px-4 py-8 text-center text-[13px] text-tg-muted">
            No chats — seed a scenario from the sidebar.
          </div>
        ) : (
          chats.map((chat) => {
            const messages = snapshot?.messages[String(chat.id)] ?? [];
            const last = messages[messages.length - 1];
            const preview = last
              ? last.deleted
                ? "Message deleted"
                : truncate(last.text ?? (last.media ? `[${last.media}]` : ""), 42)
              : "No messages yet";
            const active = chat.id === chatId;

            return (
              <button
                key={chat.id}
                type="button"
                onClick={() => onSelectChat(chat.id)}
                className={`flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors ${
                  active ? "bg-tg-hover" : "hover:bg-tg-hover/60"
                }`}
              >
                <div
                  className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-[16px] font-semibold text-white"
                  style={{ backgroundColor: colorForId(chat.id) }}
                >
                  {initial(chat.title)}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="truncate text-[14px] font-medium">{chat.title}</span>
                    {last && <span className="shrink-0 text-[11px] text-tg-muted">{formatTime(last.date)}</span>}
                  </div>
                  <div className="truncate text-[13px] text-tg-muted">{preview}</div>
                </div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
