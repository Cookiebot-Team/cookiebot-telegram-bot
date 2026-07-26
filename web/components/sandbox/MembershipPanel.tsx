"use client";

import { useState } from "react";
import type { Role, SandboxChat, SandboxUser } from "@/types";
import { joinChat, leaveChat, patchMember } from "@/lib/api";
import { colorForId, initials, ROLE_LABELS } from "@/lib/format";

const ROLE_OPTIONS: Role[] = ["member", "administrator", "creator", "restricted"];
const RETIRED_ROLES: Role[] = ["left", "kicked"];

/** Per-member membership controls for the currently selected chat.
 *
 * This is where the v1 defect lived — an anonymous admin got rejected and
 * told to disable a Telegram feature — so the anonymity switch is the
 * loudest thing in this panel, not a footnote: the row for the acting user's
 * own membership gets a gold ring the moment their anonymity is on.
 *
 * The self-join/added-by-another and left/kicked forks matter because the
 * doomlist and captcha features branch on exactly those: a listed user who
 * self-joins gets banned; the same account added by an existing member does
 * not (`cb_gateway/handlers/doomlist.py`'s own `on_join` skips a non-self
 * add). "Add member" below always sets `by_user_id` to the acting user for
 * that reason — it is not decorative plumbing. */
export default function MembershipPanel(props: {
  chat: SandboxChat | undefined;
  users: SandboxUser[];
  currentUserId: number | null;
  onRefresh: () => void;
  onError: (message: string) => void;
  onAction?: (label: string) => void;
}) {
  const { chat, users, currentUserId, onRefresh, onError, onAction } = props;
  const [pending, setPending] = useState<string | null>(null);
  const [addUserId, setAddUserId] = useState("");

  if (!chat) {
    return <p className="text-xs text-tg-muted">Select a chat in the middle pane to manage its members.</p>;
  }

  const userById = new Map(users.map((u) => [u.id, u]));
  const isActive = (role: Role) => !RETIRED_ROLES.includes(role);
  const activeMembers = chat.members.filter((m) => isActive(m.role));
  const formerMembers = chat.members.filter((m) => !isActive(m.role));
  const isMember = currentUserId != null && activeMembers.some((m) => m.user_id === currentUserId);
  const nonMembers = users.filter(
    (u) => !u.is_bot && !chat.members.some((m) => m.user_id === u.id && isActive(m.role)),
  );

  async function run(key: string, label: string, action: () => Promise<unknown>) {
    setPending(key);
    onAction?.(label);
    try {
      await action();
      onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  }

  function restrictionLabel(untilSeconds: number): string {
    if (untilSeconds <= 0) return "indefinite";
    return `until ${new Date(untilSeconds * 1000).toLocaleTimeString()}`;
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="truncate text-xs font-medium text-tg-muted">{chat.title}</p>
        {currentUserId != null && !isMember && (
          <button
            type="button"
            disabled={pending !== null}
            onClick={() => run("join-self", "Join (self)", () => joinChat(chat.id, { user_id: currentUserId }))}
            className="shrink-0 rounded bg-tg-green/20 px-2 py-0.5 text-[11px] font-medium text-tg-green hover:bg-tg-green/30 disabled:opacity-50"
          >
            + Join as me
          </button>
        )}
      </div>

      {currentUserId != null && nonMembers.length > 0 && (
        <div className="flex gap-1.5">
          <select
            value={addUserId}
            onChange={(e) => setAddUserId(e.target.value)}
            className="flex-1 rounded bg-tg-bg px-1.5 py-1 text-[11px]"
          >
            <option value="">Add a member (as me)…</option>
            {nonMembers.map((u) => (
              <option key={u.id} value={u.id}>
                {u.first_name} (@{u.username})
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={pending !== null || !addUserId}
            onClick={() => {
              const userId = Number(addUserId);
              run("add-member", `Add ${userById.get(userId)?.first_name ?? userId}`, () =>
                joinChat(chat.id, { user_id: userId, by_user_id: currentUserId }),
              );
              setAddUserId("");
            }}
            className="shrink-0 rounded bg-tg-accent px-2 py-0.5 text-[11px] font-medium text-white disabled:opacity-50"
            title="Added-by-another join — the doomlist/captcha fork that a self-join does not take."
          >
            Add
          </button>
        </div>
      )}

      <ul className="space-y-1.5">
        {activeMembers.length === 0 && <li className="text-xs text-tg-muted">No active members yet.</li>}
        {activeMembers.map((member) => {
          const user = userById.get(member.user_id);
          const label = user ? user.first_name : `#${member.user_id}`;
          const canBeAnonymous = member.role === "creator" || member.role === "administrator";
          const isRestricted = member.role === "restricted" && member.restricted_until >= 0;
          const isSelf = member.user_id === currentUserId;
          const key = `member-${member.user_id}`;

          return (
            <li
              key={member.user_id}
              className={`rounded-md bg-tg-hover/60 p-2 ${
                isSelf && member.anonymous ? "ring-1 ring-tg-amber" : ""
              }`}
            >
              <div className="flex items-center gap-2">
                <span
                  className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[10px] font-semibold text-white"
                  style={{ backgroundColor: colorForId(member.user_id) }}
                >
                  {user ? initials(user.first_name, user.last_name) : "?"}
                </span>
                <span className="truncate text-xs font-medium">{label}</span>
                {isSelf && <span className="shrink-0 rounded bg-tg-accent/30 px-1 text-[9px] text-tg-accent">you</span>}
                {member.anonymous && (
                  <span
                    className="shrink-0 rounded bg-tg-amber/25 px-1 text-[9px] text-tg-amber"
                    title="Posts as GroupAnonymousBot, not as this account — the case v1 got wrong."
                  >
                    anon
                  </span>
                )}
                <button
                  type="button"
                  disabled={pending !== null}
                  onClick={() =>
                    run(
                      key,
                      isSelf ? `${label} leaves` : `Kick ${label}`,
                      () =>
                        leaveChat(chat.id, {
                          user_id: member.user_id,
                          by_user_id: isSelf ? undefined : (currentUserId ?? undefined),
                        }),
                    )
                  }
                  className="ml-auto shrink-0 text-[11px] text-tg-red hover:underline disabled:opacity-50"
                  title={isSelf ? "Leave (Telegram 'left' status)" : "Kick (Telegram 'kicked' status, from = you)"}
                >
                  {isSelf ? "leave" : "kick"}
                </button>
              </div>

              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                <select
                  value={member.role}
                  disabled={pending !== null}
                  onChange={(e) =>
                    run(key, `${label} -> ${e.target.value}`, () =>
                      patchMember(chat.id, member.user_id, { role: e.target.value as Role }),
                    )
                  }
                  className="rounded bg-tg-bg px-1.5 py-0.5 text-[11px] text-tg-text"
                >
                  {ROLE_OPTIONS.map((role) => (
                    <option key={role} value={role}>
                      {ROLE_LABELS[role]}
                    </option>
                  ))}
                </select>

                {isRestricted && (
                  <span className="rounded bg-tg-red/15 px-1.5 py-0.5 text-[10px] text-tg-red" title="Set by the bot's own restrictChatMember call, or manually via the role select above.">
                    restricted {restrictionLabel(member.restricted_until)}
                  </span>
                )}

                {canBeAnonymous && (
                  <label
                    className={`flex cursor-pointer items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-semibold transition-colors ${
                      member.anonymous ? "bg-tg-amber text-black" : "bg-tg-bg text-tg-muted hover:text-tg-text"
                    }`}
                    title="Posts as the group (GroupAnonymousBot), not as this user. This is the case v1 got wrong: an anonymous admin was rejected and told to disable the feature."
                  >
                    <input
                      type="checkbox"
                      className="hidden"
                      checked={member.anonymous}
                      disabled={pending !== null}
                      onChange={(e) =>
                        run(key, `${label} anonymous ${e.target.checked ? "on" : "off"}`, () =>
                          patchMember(chat.id, member.user_id, { anonymous: e.target.checked }),
                        )
                      }
                    />
                    {member.anonymous ? "Anonymous ON" : "Anonymous off"}
                  </label>
                )}
              </div>
            </li>
          );
        })}
      </ul>

      {formerMembers.length > 0 && (
        <details className="rounded-md bg-tg-bg/40 p-1.5">
          <summary className="cursor-pointer text-[11px] text-tg-muted">
            Former members ({formerMembers.length})
          </summary>
          <ul className="mt-1 space-y-1">
            {formerMembers.map((member) => {
              const user = userById.get(member.user_id);
              const label = user ? user.first_name : `#${member.user_id}`;
              return (
                <li key={member.user_id} className="flex items-center justify-between gap-2 text-[11px] text-tg-muted">
                  <span>
                    {label} — {ROLE_LABELS[member.role]}
                  </span>
                  <button
                    type="button"
                    disabled={pending !== null}
                    onClick={() =>
                      run(`restore-${member.user_id}`, `Restore ${label}`, () =>
                        patchMember(chat.id, member.user_id, { role: "member" }),
                      )
                    }
                    className="text-tg-accent hover:underline disabled:opacity-50"
                  >
                    restore
                  </button>
                </li>
              );
            })}
          </ul>
        </details>
      )}
    </div>
  );
}
