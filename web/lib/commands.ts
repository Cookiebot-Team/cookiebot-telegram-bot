// Grouping and search over the command palette.
//
// The commands themselves come from `GET /api/kit` — generated from the bot's
// own parser, ideally, so a newly added alias appears without anyone retyping
// it. Nothing in this file invents a command name; it only sorts, groups and
// filters what the server already resolved.

import type { FeatureStatus, SandboxCommand } from "@/types";

// Ordered so a tester scans "will do something" before "will do nothing" —
// `partial` before `planned` because a partial feature at least replies.
const STATUS_ORDER: string[] = ["done", "partial", "planned", "blocked", "unknown"];

export const STATUS_LABEL: Record<string, string> = {
  done: "Implemented",
  partial: "Partial",
  planned: "Not implemented — expect silence",
  blocked: "Blocked",
  unknown: "Unclassified",
};

/** A status the config used but this client has no wording for still needs a
 * heading — show the raw word rather than dropping the group. */
export function statusLabel(status: FeatureStatus): string {
  return STATUS_LABEL[status] ?? status;
}

export function commandsByStatus(commands: SandboxCommand[]): [FeatureStatus, SandboxCommand[]][] {
  const groups = new Map<string, SandboxCommand[]>();
  for (const command of commands) {
    const bucket = groups.get(command.status);
    if (bucket) bucket.push(command);
    else groups.set(command.status, [command]);
  }
  const known = STATUS_ORDER.filter((status) => groups.has(status));
  const unknown = [...groups.keys()].filter((status) => !STATUS_ORDER.includes(status)).sort();
  return [...known, ...unknown].map((status) => [status, groups.get(status)!]);
}

export function searchCommands(commands: SandboxCommand[], query: string): SandboxCommand[] {
  const q = query.trim().toLowerCase().replace(/^\//, "");
  if (!q) return commands;
  return commands.filter(
    (command) =>
      command.primary.toLowerCase().includes(q) ||
      command.aliases.some((alias) => alias.toLowerCase().includes(q)) ||
      (command.title?.toLowerCase().includes(q) ?? false),
  );
}
