"use client";

import { useEffect, useMemo, useState } from "react";
import type { SandboxKit, SandboxSnapshot } from "@/types";
import UserSwitcher from "./UserSwitcher";
import MembershipPanel from "./MembershipPanel";
import CreatePanel from "./CreatePanel";
import ScenarioPanel, { type ScenarioOutcome } from "./ScenarioPanel";
import FeatureRail from "./FeatureRail";
import ScenarioRail from "./ScenarioRail";
import ApiCallLog from "./ApiCallLog";
import CommandPalette from "./CommandPalette";
import { ALL_FEATURES, ALL_SCENARIOS, isFiltered, matchesLens, type Lens } from "@/lib/lens";

/** The right-hand rail, top to bottom in the order the questions are actually
 * asked:
 *
 *   Features  — is this behaviour correct, across every check that touched it
 *   Scenario  — which individual check, and what did it record
 *   Who am I  — act as someone
 *   Members   — change the world around them
 *   Create    — add to it
 *   Seed data — start over from a known world
 *   Commands  — what can I even send
 *   What the bot did — the API-call log, this tool's main validation surface
 *
 * `currentUserId` and `chatId` are owned by `app/page.tsx` and only read here:
 * an earlier version kept its own copy in `useState` + localStorage, which
 * meant picking a user in this panel never actually changed who the composer
 * sent as — two independent "who am I" controls that silently disagreed.
 * There is exactly one now. */
export default function SandboxSidebar(props: {
  snapshot: SandboxSnapshot;
  kit: SandboxKit | null;
  currentUserId: number | null;
  chatId: number | null;
  lastActionAt: number | null;
  lens: Lens;
  onLensChange: (lens: Lens) => void;
  onSelectUser: (id: number) => void;
  onRefresh: () => void;
  onApplyPreset: (outcome: ScenarioOutcome) => void;
  onAction: (label: string) => void;
  onSendText: (text: string) => void;
}) {
  const {
    snapshot,
    kit,
    currentUserId,
    chatId,
    lastActionAt,
    lens,
    onLensChange,
    onSelectUser,
    onRefresh,
    onApplyPreset,
    onAction,
    onSendText,
  } = props;

  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!error) return;
    const id = setTimeout(() => setError(null), 6000);
    return () => clearTimeout(id);
  }, [error]);

  const humans = useMemo(() => snapshot.users.filter((u) => !u.is_bot), [snapshot.users]);
  const currentChat = useMemo(() => snapshot.chats.find((c) => c.id === chatId), [snapshot.chats, chatId]);
  const visibleApiCalls = useMemo(
    () => snapshot.api_calls.filter((call) => matchesLens(call.scenario_id, lens, snapshot.scenarios)),
    [snapshot.api_calls, snapshot.scenarios, lens],
  );

  return (
    <aside className="flex h-full w-full flex-col gap-3 overflow-y-auto bg-tg-panel p-3 text-tg-text">
      <div className="flex items-center justify-between">
        <h1 className="text-sm font-semibold">Sandbox controls</h1>
        {snapshot.bot && (
          <span className="text-[11px] text-tg-muted" title={kit ? `config: ${kit.config_source}` : undefined}>
            bot: @{snapshot.bot.username}
          </span>
        )}
      </div>

      {error && <div className="rounded bg-tg-red/15 px-2 py-1.5 text-[11px] text-tg-red">{error}</div>}

      {/* First, because after a run this is the first question a person has —
          not "what did test #47 do" but "is the thing it was checking still
          correct". Everything below filters to whatever is picked here. */}
      <section>
        <h2 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-tg-muted">Features</h2>
        <FeatureRail
          features={snapshot.features}
          scenarios={snapshot.scenarios}
          featureLens={lens.feature}
          scenarioLens={lens.scenario}
          onFeatureChange={(feature) => onLensChange({ ...lens, feature })}
          onScenarioChange={(scenario) => onLensChange({ ...lens, scenario })}
        />
      </section>

      <section>
        <h2 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-tg-muted">Scenario</h2>
        <ScenarioRail
          scenarios={snapshot.scenarios}
          features={snapshot.features}
          activeScenarioId={snapshot.active_scenario_id}
          featureLens={lens.feature}
          lens={lens.scenario}
          onLensChange={(scenario) => onLensChange({ ...lens, scenario })}
          onRefresh={onRefresh}
          onError={setError}
        />
      </section>

      <section>
        <h2 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-tg-muted">Who am I</h2>
        <UserSwitcher
          users={humans}
          currentChatMembers={currentChat?.members ?? []}
          currentUserId={currentUserId}
          onSelect={onSelectUser}
        />
      </section>

      <section>
        <h2 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-tg-muted">
          Members{currentChat ? ` · ${currentChat.title}` : ""}
        </h2>
        <MembershipPanel
          chat={currentChat}
          users={snapshot.users}
          currentUserId={currentUserId}
          onRefresh={onRefresh}
          onError={setError}
          onAction={onAction}
        />
      </section>

      <section>
        <h2 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-tg-muted">Create</h2>
        <CreatePanel
          chats={snapshot.chats}
          currentUserId={currentUserId}
          onRefresh={onRefresh}
          onError={setError}
          onAction={onAction}
        />
      </section>

      {/* Seed presets — a starting *world* (users, a group, who's a member),
          from the bot's own config. A different thing from the Scenario
          section above, which owns the word for tagged activity. */}
      <section>
        <h2 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-tg-muted">Seed data</h2>
        <ScenarioPanel kit={kit} onApplyPreset={onApplyPreset} onError={setError} />
      </section>

      <CommandPalette
        commands={kit?.commands ?? []}
        featureId={lens.feature !== ALL_FEATURES ? lens.feature : null}
        disabled={currentUserId === null || chatId === null}
        disabledReason="Pick a user and a chat first."
        onSend={(text) => {
          onAction(text);
          onSendText(text);
        }}
      />

      <section className="flex min-h-0 flex-1 flex-col">
        <h2 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-tg-muted">
          What the bot did
          {isFiltered(lens) && (
            <span className="ml-1 normal-case text-tg-amber">
              (filtered — {visibleApiCalls.length} of {snapshot.api_calls.length})
            </span>
          )}
        </h2>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <ApiCallLog
            calls={visibleApiCalls}
            lastActionAt={lastActionAt}
            scenarios={snapshot.scenarios}
            showScenarioTags={lens.scenario === ALL_SCENARIOS}
          />
        </div>
      </section>
    </aside>
  );
}
