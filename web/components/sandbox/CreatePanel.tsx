"use client";

import { useState } from "react";
import type { SandboxChat } from "@/types";
import { createChat, createUser, joinChat, openDm } from "@/lib/api";

const LANGUAGES = ["en", "pt", "es", "de", "fr"];

/** "Create things": a new user, a new group or private chat, joining the acting
 * user to any chat (not just the one currently open in the middle pane), and
 * opening the acting user's DM with the bot.
 *
 * The two private-chat controls are not the same thing, and the difference is
 * the one people lose an afternoon to. "Private" on the group form makes a chat
 * whose `type` is private, for driving a command gated on
 * `chat.type === "private"` — the bot cannot send into it, because its id comes
 * from the chat counter. "Open DM" makes the chat whose id *is* the user's id,
 * which is the only one a handler answering privately can reach. */
export default function CreatePanel(props: {
  chats: SandboxChat[];
  currentUserId: number | null;
  onRefresh: () => void;
  onError: (message: string) => void;
  onAction?: (label: string) => void;
}) {
  const { chats, currentUserId, onRefresh, onError, onAction } = props;

  const [firstName, setFirstName] = useState("");
  const [username, setUsername] = useState("");
  const [language, setLanguage] = useState("en");
  const [groupTitle, setGroupTitle] = useState("");
  const [groupPrivate, setGroupPrivate] = useState(false);
  const [joinChatId, setJoinChatId] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  async function run(key: string, action: () => Promise<unknown>, after?: () => void) {
    setBusy(key);
    try {
      await action();
      after?.();
      onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-3">
      <form
        className="space-y-1.5"
        onSubmit={(e) => {
          e.preventDefault();
          if (!firstName.trim() || !username.trim()) return;
          run(
            "user",
            () =>
              createUser({
                first_name: firstName.trim(),
                username: username.trim(),
                language_code: language,
              }),
            () => {
              setFirstName("");
              setUsername("");
            },
          );
        }}
      >
        <p className="text-[11px] font-semibold uppercase tracking-wide text-tg-muted">New user</p>
        <div className="flex gap-1.5">
          <input
            value={firstName}
            onChange={(e) => setFirstName(e.target.value)}
            placeholder="First name"
            className="w-1/2 rounded bg-tg-hover px-2 py-1 text-xs placeholder:text-tg-muted"
          />
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="username"
            className="w-1/2 rounded bg-tg-hover px-2 py-1 text-xs placeholder:text-tg-muted"
          />
        </div>
        <div className="flex gap-1.5">
          <select
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
            className="rounded bg-tg-hover px-2 py-1 text-xs"
          >
            {LANGUAGES.map((lang) => (
              <option key={lang} value={lang}>
                {lang}
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={busy !== null || !firstName.trim() || !username.trim()}
            className="flex-1 rounded bg-tg-accent px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
          >
            Create user
          </button>
        </div>
      </form>

      <form
        className="space-y-1.5"
        onSubmit={(e) => {
          e.preventDefault();
          if (!groupTitle.trim()) return;
          run(
            "group",
            () => createChat({ title: groupTitle.trim(), type: groupPrivate ? "private" : "supergroup" }),
            () => setGroupTitle(""),
          );
        }}
      >
        <p className="text-[11px] font-semibold uppercase tracking-wide text-tg-muted">New group / chat</p>
        <div className="flex gap-1.5">
          <input
            value={groupTitle}
            onChange={(e) => setGroupTitle(e.target.value)}
            placeholder="Title"
            className="flex-1 rounded bg-tg-hover px-2 py-1 text-xs placeholder:text-tg-muted"
          />
          <label className="flex shrink-0 items-center gap-1 text-[11px] text-tg-muted" title="For testing a chat.type === 'private' command; the bot still can't DM an existing member through this.">
            <input type="checkbox" checked={groupPrivate} onChange={(e) => setGroupPrivate(e.target.checked)} />
            private
          </label>
          <button
            type="submit"
            disabled={busy !== null || !groupTitle.trim()}
            className="shrink-0 rounded bg-tg-accent px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
          >
            Create
          </button>
        </div>
      </form>

      <form
        className="space-y-1.5"
        onSubmit={(e) => {
          e.preventDefault();
          const chatId = Number(joinChatId);
          if (!joinChatId || currentUserId == null || Number.isNaN(chatId)) return;
          onAction?.(`join chat ${chatId}`);
          run("join", () => joinChat(chatId, { user_id: currentUserId }));
        }}
      >
        <p className="text-[11px] font-semibold uppercase tracking-wide text-tg-muted">Join current user to a chat</p>
        <div className="flex gap-1.5">
          <select
            value={joinChatId}
            onChange={(e) => setJoinChatId(e.target.value)}
            className="flex-1 rounded bg-tg-hover px-2 py-1 text-xs"
          >
            <option value="">Pick a chat…</option>
            {chats.map((chat) => (
              <option key={chat.id} value={chat.id}>
                {chat.title}
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={busy !== null || currentUserId == null || !joinChatId}
            className="rounded bg-tg-accent px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
          >
            Join
          </button>
        </div>
        {currentUserId == null && <p className="text-[10px] text-tg-muted">Pick a user above first.</p>}
      </form>

      <div className="space-y-1.5">
        <p className="text-[11px] font-semibold uppercase tracking-wide text-tg-muted">
          Private chat with the bot
        </p>
        <button
          type="button"
          disabled={busy !== null || currentUserId == null}
          onClick={() => {
            if (currentUserId == null) return;
            onAction?.("open DM");
            run("dm", () => openDm(currentUserId));
          }}
          className="w-full rounded bg-tg-accent px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
        >
          Open DM as current user
        </button>
        <p className="text-[10px] text-tg-muted">
          Stands for pressing Start. Until it exists the bot&apos;s private replies fail with
          Telegram&apos;s own <span className="font-mono">403 Forbidden</span> — which is what real
          Telegram does, not a sandbox gap.
        </p>
      </div>
    </div>
  );
}
