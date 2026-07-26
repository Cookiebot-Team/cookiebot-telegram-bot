"use client";

// Seed data: which world to start from, and the one-click presets that put a
// tester in front of a specific question.
//
// Both come from `GET /api/kit` (`sandbox.config.json`), not from this file.
// That is the whole reason this component is short: the presets used to be a
// hardcoded array naming one particular bot's features, its users, and its
// known defects, which meant the workbench could only ever be that bot's
// workbench. Now a preset is data — seed a world, act as someone, here is
// what to do and what to watch for — and this renders whatever the bot's
// config declares.
//
// A preset deliberately does not assert its own outcome. The bot's real
// reaction *is* the result; a preset that graded itself would be testing its
// author's expectations rather than the bot.

import { useState } from "react";
import type { SandboxKit, SandboxPreset, SandboxSnapshot } from "@/types";
import { createUser, reset, seed } from "@/lib/api";

export interface ScenarioOutcome {
  label: string;
  whatToDo: string;
  whatToLookFor: string;
  snapshot: SandboxSnapshot;
  actingUserId: number | null;
  chatId: number | null;
  /** Which feature this preset is about, so applying one can point the
   * feature lens at it — pressing "check the captcha" should leave the
   * workbench filtered to the captcha, not to everything. */
  featureId: string | null;
}

function firstChatId(snapshot: SandboxSnapshot): number | null {
  return snapshot.chats.length > 0 ? snapshot.chats[0].id : null;
}

/** A preset names its acting user by the seed's own key (`"carol"`), which is
 * also the username the seed creates — so a username lookup resolves both,
 * and a config that uses a key different from the username still works as
 * long as they agree. Falls back to the first human so a preset with a typo
 * still lands the tester somewhere they can act. */
function resolveActingUser(snapshot: SandboxSnapshot, key: string | null): number | null {
  if (key) {
    const match = snapshot.users.find((u) => u.username === key || u.first_name === key);
    if (match) return match.id;
  }
  return snapshot.users.find((u) => !u.is_bot)?.id ?? null;
}

async function runPreset(preset: SandboxPreset): Promise<ScenarioOutcome> {
  const snapshot = await seed(preset.seed);
  let actingUserId = resolveActingUser(snapshot, preset.acting_user);

  if (preset.create_user) {
    // A "brand new account" preset: the point is that this account has no
    // history in the chat, so it has to be minted now rather than seeded —
    // a seeded one would already have been sitting there when the world was
    // built, which is a different situation entirely.
    const suffix = String(Date.now() % 100000);
    const created = await createUser({
      first_name: preset.create_user.first_name ?? "Newcomer",
      username: `${preset.create_user.username_prefix ?? "newcomer"}${suffix}`,
    });
    actingUserId = created.id;
  }

  return {
    label: preset.label || preset.button,
    whatToDo: preset.what_to_do,
    whatToLookFor: preset.what_to_look_for,
    snapshot,
    actingUserId,
    chatId: firstChatId(snapshot),
    featureId: preset.feature_id,
  };
}

/** Presets plus the plain seed/reset controls. The "currently loaded" banner
 * this drives lives in the parent (`app/page.tsx`) so it can sit next to the
 * chat pane rather than buried in the sidebar — "which scenario am I looking
 * at" needs to survive scrolling the member list. */
export default function ScenarioPanel(props: {
  kit: SandboxKit | null;
  onApplyPreset: (outcome: ScenarioOutcome) => void;
  onError: (message: string) => void;
}) {
  const { kit, onApplyPreset, onError } = props;
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmingReset, setConfirmingReset] = useState(false);

  async function run(key: string, action: () => Promise<ScenarioOutcome>) {
    setBusy(key);
    try {
      onApplyPreset(await action());
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function bareSeed(name: string, label: string): Promise<ScenarioOutcome> {
    const snapshot = await seed(name);
    return {
      label,
      whatToDo: "Build the scenario by hand — create users, join them, send messages.",
      whatToLookFor: "Whatever the action you're about to take should produce.",
      snapshot,
      actingUserId: snapshot.users.find((u) => !u.is_bot)?.id ?? null,
      chatId: firstChatId(snapshot),
      featureId: null,
    };
  }

  async function runReset(): Promise<ScenarioOutcome> {
    const snapshot = await reset();
    return {
      label: "Default (reset)",
      whatToDo: "Pick a user and a chat, then drive the bot.",
      whatToLookFor: "—",
      snapshot,
      actingUserId: snapshot.users.find((u) => !u.is_bot)?.id ?? null,
      chatId: firstChatId(snapshot),
      featureId: null,
    };
  }

  const presets = kit?.presets ?? [];
  const seeds = kit?.seeds ?? [];

  return (
    <div className="space-y-2">
      {presets.length > 0 && (
        <div className="space-y-1">
          {presets.map((preset) => (
            <button
              key={preset.id}
              type="button"
              disabled={busy !== null}
              onClick={() => run(preset.id, () => runPreset(preset))}
              title={preset.what_to_do}
              className="w-full rounded bg-tg-hover px-2 py-1.5 text-left text-xs font-medium hover:bg-tg-hover/70 disabled:opacity-50"
            >
              {busy === preset.id ? "Setting up…" : preset.button}
            </button>
          ))}
        </div>
      )}

      {/* Every configured world, not a fixed three: which starting states are
          worth one click is a fact about the bot under test. */}
      <div className="flex flex-wrap gap-1.5 border-t border-tg-divider pt-1.5">
        {seeds.map((fixture) => (
          <button
            key={fixture.name}
            type="button"
            disabled={busy !== null}
            onClick={() => run(fixture.name, () => bareSeed(fixture.name, fixture.title))}
            title={fixture.description}
            className="flex-1 rounded bg-tg-hover/60 px-2 py-1 text-[11px] hover:bg-tg-hover disabled:opacity-50"
          >
            {fixture.name}
          </button>
        ))}
      </div>

      {!confirmingReset ? (
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => setConfirmingReset(true)}
          className="w-full rounded border border-tg-red/40 px-2 py-1.5 text-xs font-medium text-tg-red hover:bg-tg-red/10 disabled:opacity-50"
        >
          Reset sandbox <span className="opacity-60">(Alt+R)</span>
        </button>
      ) : (
        <div className="flex gap-1.5">
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => {
              setConfirmingReset(false);
              void run("reset", runReset);
            }}
            className="flex-1 rounded bg-tg-red px-2 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
          >
            Confirm — wipe everything
          </button>
          <button type="button" onClick={() => setConfirmingReset(false)} className="rounded bg-tg-hover px-2 py-1.5 text-xs">
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

/** Exposed so `app/page.tsx` can trigger the same reset from the global
 * Alt+R shortcut without duplicating the confirm-dialog UX inline. */
export async function confirmAndReset(onApplyPreset: (outcome: ScenarioOutcome) => void): Promise<void> {
  if (!window.confirm("Reset the sandbox? This wipes every user, chat and message.")) return;
  const snapshot = await reset();
  onApplyPreset({
    label: "Default (reset)",
    whatToDo: "Pick a user and a chat, then drive the bot.",
    whatToLookFor: "—",
    snapshot,
    actingUserId: snapshot.users.find((u) => !u.is_bot)?.id ?? null,
    chatId: firstChatId(snapshot),
    featureId: null,
  });
}
