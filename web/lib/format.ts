// Small, dependency-free formatting helpers shared across the chat and
// sandbox-control components. One copy: `web/components/sandbox/format.ts`
// used to duplicate the avatar-colour and initials logic with a different
// palette and a different call signature, written by an agent that didn't
// know this file existed. Everything here is the merged, single version —
// every caller in `components/sandbox/` now imports from here.

import type { Role, SandboxUser } from "@/types";

export function formatTime(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function isSameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

export function dayKey(unixSeconds: number): string {
  const d = new Date(unixSeconds * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

/** "Today" / "Yesterday" / "25 July 2026" — Telegram's date-separator labels. */
export function formatDateLabel(unixSeconds: number): string {
  const d = new Date(unixSeconds * 1000);
  const now = new Date();
  if (isSameDay(d, now)) return "Today";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (isSameDay(d, yesterday)) return "Yesterday";
  return d.toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
}

/** `at` fields from the sandbox server are Python `time.time()` — Unix
 *  seconds, not milliseconds. */
export function relativeTime(atSeconds: number, nowMs: number = Date.now()): string {
  const deltaS = Math.max(0, Math.round(nowMs / 1000 - atSeconds));
  if (deltaS < 1) return "just now";
  if (deltaS < 60) return `${deltaS}s ago`;
  const deltaM = Math.round(deltaS / 60);
  if (deltaM < 60) return `${deltaM}m ago`;
  const deltaH = Math.round(deltaM / 60);
  if (deltaH < 24) return `${deltaH}h ago`;
  const deltaD = Math.round(deltaH / 24);
  return `${deltaD}d ago`;
}

/** Milliseconds between two `time.time()`-style Unix-second timestamps,
 * rendered as "0.4s" / "820ms" — used for "how long did the bot take to
 * answer", where relative-time's minute granularity is too coarse. */
export function formatDuration(fromSeconds: number, toSeconds: number): string {
  const ms = Math.max(0, Math.round((toSeconds - fromSeconds) * 1000));
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** "12m 34s" / "1h 03m" — how long a scenario ran. Coarser than
 * `formatDuration` on purpose: that one is built for bot latency (sub-second
 * to a few seconds), this one for a run a tester might leave going for an
 * afternoon. */
export function formatElapsed(fromSeconds: number, toSeconds: number): string {
  const totalS = Math.max(0, Math.round(toSeconds - fromSeconds));
  const h = Math.floor(totalS / 3600);
  const m = Math.floor((totalS % 3600) / 60);
  const s = totalS % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

export function displayName(user: SandboxUser | null | undefined): string {
  if (!user) return "Unknown user";
  return user.last_name ? `${user.first_name} ${user.last_name}` : user.first_name;
}

/** Single-letter avatar fallback, for chat/group avatars keyed off a title. */
export function initial(label: string): string {
  const trimmed = label.trim();
  return trimmed.length > 0 ? trimmed[0].toUpperCase() : "?";
}

/** Two-letter avatar fallback, for user avatars keyed off first+last name. */
export function initials(firstName: string, lastName?: string | null): string {
  const first = firstName.trim().charAt(0);
  const last = (lastName ?? "").trim().charAt(0);
  const both = (first + last).toUpperCase();
  return both.length > 0 ? both : "?";
}

// Telegram assigns each conversation a stable colour for its avatar/name from
// a small fixed palette, keyed off the user id — no lookups, no configuration.
const NAME_PALETTE = ["#e17076", "#7bc862", "#e5ca77", "#65aadd", "#a695e7", "#ee7aae", "#6ec9cb", "#faa774"];

export function colorForId(id: number): string {
  const index = Math.abs(id) % NAME_PALETTE.length;
  return NAME_PALETTE[index];
}

export function truncate(text: string, max: number): string {
  const collapsed = text.replace(/\s+/g, " ").trim();
  return collapsed.length > max ? `${collapsed.slice(0, max - 1)}…` : collapsed;
}

export const ROLE_LABELS: Record<Role, string> = {
  creator: "Creator",
  administrator: "Admin",
  member: "Member",
  restricted: "Restricted",
  kicked: "Kicked",
  left: "Left",
};
