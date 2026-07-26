"use client";

import { useMemo, useState } from "react";
import { commandsByStatus, searchCommands, statusLabel } from "@/lib/commands";
import type { FeatureStatus, SandboxCommand } from "@/types";

const STATUS_DOT: Record<string, string> = {
  done: "bg-tg-green",
  partial: "bg-tg-amber",
  planned: "bg-tg-muted",
  blocked: "bg-tg-red",
};

/** Every command the bot's parser recognises, served by `GET /api/kit` rather
 * than hand-typed here — so a newly added alias or a newly finished feature
 * shows up without this file changing, and so the palette belongs to the bot
 * under test rather than to this client.
 *
 * One click sends the command's primary trigger as the acting user in the
 * open chat. A "planned" command is greyed out and labelled, so silence reads
 * as expected rather than as a missed bug — which is the difference between a
 * tester filing a real defect and filing noise.
 *
 * Filtering by feature is deliberate: it is the same axis the feature rail
 * above groups scenarios by, so "what can I even send to exercise this" and
 * "what did the last run prove about it" line up. */
export default function CommandPalette(props: {
  commands: SandboxCommand[];
  disabled: boolean;
  disabledReason?: string;
  /** When the workbench is filtered to a feature, offer that feature's
   * commands first — the tester has already said what they are working on. */
  featureId?: string | null;
  onSend: (text: string) => void;
}) {
  const { commands, disabled, disabledReason, featureId, onSend } = props;
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [onlyThisFeature, setOnlyThisFeature] = useState(true);

  const scoped = useMemo(() => {
    if (!featureId || !onlyThisFeature) return commands;
    const mine = commands.filter((c) => c.feature_id === featureId);
    // A feature with no commands of its own (a join-time check, a background
    // job) must not produce an empty palette — fall back to everything rather
    // than showing a tester a list with nothing in it and no explanation.
    return mine.length > 0 ? mine : commands;
  }, [commands, featureId, onlyThisFeature]);

  const groups = useMemo(
    () =>
      query.trim()
        ? [["filtered", searchCommands(scoped, query)] as const]
        : commandsByStatus(scoped),
    [scoped, query],
  );

  const scopedToFeature = Boolean(featureId) && onlyThisFeature && scoped.length < commands.length;

  return (
    <div className="border-t border-tg-divider pt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between text-[11px] font-semibold uppercase tracking-wide text-tg-muted"
      >
        <span>Commands ({scoped.length})</span>
        <span>{open ? "−" : "+"}</span>
      </button>

      {open && (
        <div className="mt-1.5 space-y-2">
          {disabled && (
            <p className="text-[11px] text-tg-muted">{disabledReason ?? "Pick a user and a chat first."}</p>
          )}
          {commands.length === 0 && (
            <p className="text-[11px] text-tg-muted">
              No commands declared. Add a <code>commands</code> list to the sandbox config to get a
              palette — generating it from the bot&rsquo;s own parser keeps it from drifting.
            </p>
          )}
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter…"
            className="w-full rounded bg-tg-hover px-2 py-1 text-xs placeholder:text-tg-muted"
          />
          {featureId && (
            <button
              type="button"
              onClick={() => setOnlyThisFeature((v) => !v)}
              className={`w-full rounded px-2 py-0.5 text-left text-[10px] ${
                scopedToFeature ? "bg-tg-amber/20 text-tg-amber" : "bg-tg-hover text-tg-muted"
              }`}
            >
              {scopedToFeature ? "Showing only this feature's commands" : "Showing every command"}
            </button>
          )}
          <div className="max-h-72 space-y-2 overflow-y-auto">
            {groups.map(([status, group]) => (
              <div key={status}>
                {status !== "filtered" && (
                  <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-tg-muted">
                    {statusLabel(status as FeatureStatus)}
                  </p>
                )}
                <ul className="space-y-1">
                  {group.map((command) => (
                    <CommandRow key={command.canonical} command={command} disabled={disabled} onSend={onSend} />
                  ))}
                </ul>
              </div>
            ))}
            {groups.every(([, group]) => group.length === 0) && (
              <p className="text-xs text-tg-muted">No command matches &ldquo;{query}&rdquo;.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function CommandRow(props: { command: SandboxCommand; disabled: boolean; onSend: (text: string) => void }) {
  const { command, disabled, onSend } = props;
  return (
    <li className="group flex items-start gap-1.5 rounded px-1 py-0.5 hover:bg-tg-hover/60">
      <span
        className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${STATUS_DOT[command.status] ?? "bg-tg-muted"}`}
        title={command.status}
      />
      <div className="min-w-0 flex-1">
        <button
          type="button"
          disabled={disabled}
          onClick={() => onSend(command.primary)}
          title={`Send ${command.primary}`}
          className="rounded bg-tg-hover px-1 font-mono text-tg-accent hover:bg-tg-accent hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          {command.primary}
        </button>
        {command.aliases.length > 0 && (
          <span className="ml-1 text-[10px] text-tg-muted">
            aka {command.aliases.slice(0, 3).join(", ")}
            {command.aliases.length > 3 ? "…" : ""}
          </span>
        )}
        {(command.title ?? command.hint) && (
          <div className="text-[11px] text-tg-muted">{command.hint ?? command.title}</div>
        )}
      </div>
    </li>
  );
}
