"use client";

import type { Membership, SandboxUser } from "@/types";
import { colorForId, initials } from "@/lib/format";

/** "Who am I" — the single most-used control in the sandbox, and the one
 * that has to be impossible to miss: every send, join, and command goes out
 * as whichever chip is highlighted here. A chip for a member whose anonymity
 * is switched on in the open chat gets a gold "anon" badge, because acting as
 * an anonymous admin — and confirming the bot still shows them the config
 * menu — is the single case this sandbox exists to reproduce; v1 rejected it. */
export default function UserSwitcher(props: {
  users: SandboxUser[];
  currentChatMembers: Membership[];
  currentUserId: number | null;
  onSelect: (id: number) => void;
}) {
  const { users, currentChatMembers, currentUserId, onSelect } = props;

  if (users.length === 0) {
    return <p className="text-xs text-tg-muted">No users yet — create one below or seed a scenario.</p>;
  }

  const membershipById = new Map(currentChatMembers.map((m) => [m.user_id, m]));

  return (
    <div className="flex flex-wrap gap-1.5">
      {users.map((user, index) => {
        const active = user.id === currentUserId;
        const membership = membershipById.get(user.id);
        const isAnonAdmin = membership?.anonymous === true;
        return (
          <button
            key={user.id}
            type="button"
            onClick={() => onSelect(user.id)}
            title={`${user.first_name}${user.last_name ? ` ${user.last_name}` : ""} · @${user.username} · ${user.language_code}${
              isAnonAdmin ? " · anonymous admin in this chat" : ""
            } (Alt+${((index % 9) + 1)} switches here)`}
            aria-pressed={active}
            data-user-switch-index={index % 9}
            className={`flex items-center gap-1.5 rounded-full py-1 pl-1 pr-2.5 text-xs transition-colors ${
              active ? "bg-tg-accent text-white" : "bg-tg-hover text-tg-text hover:bg-tg-hover/70"
            } ${isAnonAdmin && !active ? "ring-1 ring-tg-amber" : ""}`}
          >
            <span
              className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold text-white"
              style={{ backgroundColor: colorForId(user.id) }}
            >
              {initials(user.first_name, user.last_name)}
            </span>
            <span className="font-medium">{user.first_name}</span>
            <span className="text-[9px] uppercase opacity-70">{user.language_code}</span>
            {isAnonAdmin && (
              <span className={`rounded px-1 text-[9px] font-bold ${active ? "bg-white/25" : "bg-tg-amber text-black"}`}>
                anon
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
