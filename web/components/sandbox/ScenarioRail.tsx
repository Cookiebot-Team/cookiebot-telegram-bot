"use client";

// The scenario lens: filter the workbench down to one named span of activity,
// see what it was trying to prove, and (for a human doing UAT by hand) record
// one of your own.
//
// This is a different "scenario" from `ScenarioPanel.tsx`'s seed presets —
// that one picks *starting data* (a `SandboxSeed`, a fixed set of
// users/chats); this one is the server's `Scenario`, a tag every message and
// API call carries while it's the active one. After a test run or an
// afternoon of manual testing the sandbox holds traffic from dozens of both
// kinds of checks with nothing distinguishing one from another — this
// component is the fix: pick a scenario, see only its messages and API calls;
// pick none, see the badge on each row telling you which check produced it.
//
// It sits *below* `FeatureRail.tsx`, and its picker only offers scenarios
// belonging to whatever feature is selected there. That ordering is the point:
// "which feature is broken" comes before "which of its checks failed", and a
// picker that always listed all 200 scenarios would make the second question
// the only one you could ask.
//
// Three things live here, top to bottom:
//   1. "Recording" — whichever scenario is currently active (tagging new
//      traffic), with the note/pass/fail controls a manual tester uses
//      mid-flow. Always mounted so the Alt+N/Alt+P/Alt+F shortcuts in
//      `app/page.tsx` have a fixed target regardless of what's selected below.
//   2. The picker itself — which scenario (or "all", or "untagged") the
//      timeline and API log are filtered to, within the selected feature.
//   3. The detail view for whatever the lens is currently pointed at: status,
//      feature, description, tags, duration, counts, its notes timeline, and
//      its free-form metadata.

import { useEffect, useMemo, useState } from "react";
import type { Feature, Scenario, ScenarioNoteLevel, ScenarioStatus } from "@/types";
import { activateScenario, addScenarioNote, createScenario, deactivateScenario, endScenario } from "@/lib/api";
import {
  ALL_FEATURES,
  ALL_SCENARIOS,
  UNTAGGED_SCENARIO,
  featureLabel,
  scenariosInFeature,
  type FeatureLens,
  type ScenarioLens,
} from "@/lib/lens";
import { formatElapsed, relativeTime } from "@/lib/format";

// `status` is a free-form string server-side (see `types.ts`'s `ScenarioStatus`
// docstring) — the three below are this client's own convention (what the
// pass/fail controls write, plus the server's own "running"/"closed"
// defaults) and get a treatment tuned for "read the outcome at a glance".
// Anything else the e2e suite or a human types in still has to render, so it
// falls back to a plain neutral pill showing the word verbatim.
const KNOWN_STATUS_STYLES: Record<string, { label: string; className: string; pulse?: boolean }> = {
  running: { label: "Running", className: "bg-tg-accent/20 text-tg-accent", pulse: true },
  passed: { label: "Passed", className: "bg-tg-green/20 text-tg-green" },
  failed: { label: "Failed", className: "bg-tg-red/20 text-tg-red" },
  skipped: { label: "Skipped", className: "bg-tg-muted/20 text-tg-muted" },
  closed: { label: "Closed", className: "bg-tg-muted/20 text-tg-muted" },
};

/** Small, glance-legible status pill — the three statuses a person actually
 * scans for (running/passed/failed) get the loudest treatment; anything else
 * (skipped/closed, or a word the e2e suite invented) gets a muted fallback
 * that still shows the exact string rather than hiding it behind "unknown". */
export function ScenarioStatusBadge({ status }: { status: ScenarioStatus }) {
  const style = KNOWN_STATUS_STYLES[status] ?? { label: status, className: "bg-tg-muted/20 text-tg-muted" };
  return (
    <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${style.className}`}>
      {style.pulse && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />}
      {style.label}
    </span>
  );
}

const NOTE_LEVEL_STYLES: Record<ScenarioNoteLevel, string> = {
  info: "text-tg-muted",
  warn: "text-tg-amber",
  error: "text-tg-red",
};

/** Renders whatever is in a scenario's free-form `metadata` — the e2e
 * runner's test nodeid and source file today, whatever else tomorrow. The
 * shape isn't known in advance (nested objects/arrays included), so this
 * recurses on the value's own runtime type instead of assuming a schema. */
function MetadataValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="text-tg-muted">—</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-tg-muted">[]</span>;
    return (
      <ul className="ml-3 list-disc space-y-0.5 marker:text-tg-muted">
        {value.map((item, index) => (
          <li key={index}>
            <MetadataValue value={item} />
          </li>
        ))}
      </ul>
    );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <span className="text-tg-muted">{"{}"}</span>;
    return (
      <dl className="ml-3 space-y-0.5">
        {entries.map(([key, entryValue]) => (
          <div key={key} className="flex gap-1.5">
            <dt className="shrink-0 text-tg-muted">{key}:</dt>
            <dd className="min-w-0 break-words">
              <MetadataValue value={entryValue} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <span className="break-words font-mono text-tg-text/90">{String(value)}</span>;
}

function ScenarioDetail(props: {
  scenario: Scenario;
  features: Feature[];
  isActive: boolean;
  onRefresh: () => void;
  onError: (message: string) => void;
}) {
  const { scenario, features, isActive, onRefresh, onError } = props;
  const [busy, setBusy] = useState<string | null>(null);
  const [, forceTick] = useState(0);

  // A running scenario's own "how long so far" needs to keep counting even
  // though nothing about the scenario object itself changes between polls.
  useEffect(() => {
    if (scenario.status !== "running") return;
    const id = setInterval(() => forceTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [scenario.status]);

  // Deliberately doesn't call `onAction` — that feeds `app/page.tsx`'s "did
  // the bot answer yet" indicator, and none of these calls provoke a bot
  // reply. Doing so anyway would leave that indicator stuck on "waiting"
  // forever after, say, ending a scenario, which is a false signal worse
  // than not having one.
  async function run(key: string, action: () => Promise<unknown>) {
    setBusy(key);
    try {
      await action();
      onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const metadataEntries = Object.entries(scenario.metadata);

  return (
    <div className="space-y-2 rounded-md bg-tg-hover/40 p-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-[13px] font-semibold">{scenario.name}</p>
          <p className="truncate text-[10px] uppercase tracking-wide text-tg-muted">
            {/* Which feature this rolls up to, stated on every scenario — a
                scenario reading "unfiled" is a check whose result reaches no
                feature summary, which is worth noticing here rather than
                discovering as a gap in the rollup above. */}
            {featureLabel(features, scenario.feature)}
            {scenario.source && ` · ${scenario.source}`}
          </p>
        </div>
        <ScenarioStatusBadge status={scenario.status} />
      </div>

      {scenario.description && <p className="text-[12px] text-tg-text/90">{scenario.description}</p>}

      {scenario.tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {scenario.tags.map((tag) => (
            <span key={tag} className="rounded-full bg-tg-bg px-1.5 py-0.5 text-[10px] text-tg-muted">
              {tag}
            </span>
          ))}
        </div>
      )}

      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-tg-muted">
        <span>
          ran {formatElapsed(scenario.started_at, scenario.ended_at ?? Date.now() / 1000)}
          {scenario.status === "running" ? " so far" : ""}
        </span>
        <span>started {relativeTime(scenario.started_at)}</span>
        <span>{scenario.message_count} message{scenario.message_count === 1 ? "" : "s"}</span>
        <span>{scenario.api_call_count} API call{scenario.api_call_count === 1 ? "" : "s"}</span>
      </div>

      {!isActive && scenario.status === "running" && (
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => run("activate", () => activateScenario(scenario.id))}
          className="rounded bg-tg-accent px-2 py-1 text-[11px] font-medium text-white disabled:opacity-50"
        >
          Set as active
        </button>
      )}

      {scenario.status === "running" && (
        <div className="flex gap-1.5">
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => run("end-passed", () => endScenario(scenario.id, { status: "passed" }))}
            className="flex-1 rounded bg-tg-green/20 px-2 py-1 text-[11px] font-medium text-tg-green hover:bg-tg-green/30 disabled:opacity-50"
          >
            End — passed
          </button>
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => run("end-failed", () => endScenario(scenario.id, { status: "failed" }))}
            className="flex-1 rounded bg-tg-red/20 px-2 py-1 text-[11px] font-medium text-tg-red hover:bg-tg-red/30 disabled:opacity-50"
          >
            End — failed
          </button>
        </div>
      )}

      <div>
        <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-tg-muted">
          Notes {scenario.notes.length > 0 && `(${scenario.notes.length})`}
        </p>
        {scenario.notes.length === 0 ? (
          <p className="text-[11px] text-tg-muted">No notes yet.</p>
        ) : (
          <ul className="space-y-1">
            {scenario.notes.map((note, index) => (
              <li key={index} className="text-[11px]">
                <span className={`font-semibold ${NOTE_LEVEL_STYLES[note.level]}`}>{note.level}</span>{" "}
                <span className="text-tg-muted">{relativeTime(note.at)}</span> — {note.text}
              </li>
            ))}
          </ul>
        )}
      </div>

      {metadataEntries.length > 0 && (
        <details className="rounded bg-tg-bg/40 p-1.5" open>
          <summary className="cursor-pointer text-[10px] font-semibold uppercase tracking-wide text-tg-muted">
            Metadata
          </summary>
          <div className="mt-1 text-[11px]">
            <MetadataValue value={scenario.metadata} />
          </div>
        </details>
      )}
    </div>
  );
}

export default function ScenarioRail(props: {
  scenarios: Scenario[];
  features: Feature[];
  activeScenarioId: string | null;
  featureLens: FeatureLens;
  lens: ScenarioLens;
  onLensChange: (lens: ScenarioLens) => void;
  onRefresh: () => void;
  onError: (message: string) => void;
}) {
  const { scenarios, features, activeScenarioId, featureLens, lens, onLensChange, onRefresh, onError } = props;

  const [busy, setBusy] = useState<string | null>(null);
  const [noteText, setNoteText] = useState("");
  const [noteLevel, setNoteLevel] = useState<ScenarioNoteLevel>("info");
  const [starting, setStarting] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [newFeature, setNewFeature] = useState("");
  const [newTags, setNewTags] = useState("");
  const [, forceTick] = useState(0);

  const activeScenario = useMemo(
    () => (activeScenarioId ? (scenarios.find((s) => s.id === activeScenarioId) ?? null) : null),
    [scenarios, activeScenarioId],
  );

  // The "recording for Nm" clock next to the active scenario.
  useEffect(() => {
    if (!activeScenario) return;
    const id = setInterval(() => forceTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [activeScenario]);

  // Only the selected feature's scenarios, newest first. Scoping the picker
  // to the feature above is what makes it usable after a real run: an
  // unscoped list of every scenario a suite ever recorded is a dropdown
  // nobody can find anything in.
  const newestFirst = useMemo(
    () => [...scenariosInFeature(scenarios, featureLens)].reverse(),
    [scenarios, featureLens],
  );
  const selected = lens !== ALL_SCENARIOS && lens !== UNTAGGED_SCENARIO ? (scenarios.find((s) => s.id === lens) ?? null) : null;

  async function run(key: string, action: () => Promise<unknown>) {
    setBusy(key);
    try {
      await action();
      onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  // None of these call `onAction` — see `ScenarioDetail.run`'s comment above
  // for why starting, noting, pausing or ending a scenario must not feed the
  // "did the bot answer yet" indicator.
  async function submitStart() {
    const name = newName.trim();
    if (!name) return;
    setBusy("start");
    try {
      const tags = newTags
        .split(",")
        .map((t) => t.trim())
        .filter((t) => t.length > 0);
      const created = await createScenario({
        name,
        description: newDescription.trim() || undefined,
        // Default to whatever feature is selected above: a tester who filtered
        // to a feature and then started a scenario is, essentially always,
        // checking that feature — making them restate it is how scenarios end
        // up unfiled.
        feature: newFeature || (featureLens !== ALL_FEATURES ? featureLens : undefined),
        tags: tags.length > 0 ? tags : undefined,
        source: "manual",
      });
      setNewName("");
      setNewDescription("");
      setNewFeature("");
      setNewTags("");
      setStarting(false);
      onLensChange(created.id);
      onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function submitNote() {
    if (!activeScenarioId || !noteText.trim()) return;
    await run("note", () => addScenarioNote(activeScenarioId, { text: noteText.trim(), level: noteLevel }));
    setNoteText("");
  }

  return (
    <div className="space-y-2">
      {/* Recording status + the controls a manual tester reaches for mid-flow.
          Always mounted (even with no active scenario) so Alt+N/Alt+P/Alt+F in
          `app/page.tsx` have a stable target — they act on `activeScenarioId`,
          never on whatever the lens below happens to be pointed at, so a
          keyboard shortcut can never annotate or end the wrong scenario. */}
      {activeScenario ? (
        <div className="space-y-1.5 rounded-md border border-tg-accent/40 bg-tg-accent/10 p-2">
          <div className="flex items-center gap-1.5 text-[12px]">
            <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-tg-accent" />
            <span className="min-w-0 flex-1 truncate font-medium">Recording: {activeScenario.name}</span>
            <span className="shrink-0 text-tg-muted">{formatElapsed(activeScenario.started_at, Date.now() / 1000)}</span>
          </div>
          <div className="flex gap-1.5">
            <input
              id="scenario-note-input"
              value={noteText}
              onChange={(e) => setNoteText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void submitNote();
                }
              }}
              placeholder="Add a note (Alt+N)"
              className="min-w-0 flex-1 rounded bg-tg-bg px-2 py-1 text-[11px] placeholder:text-tg-muted"
            />
            <select
              value={noteLevel}
              onChange={(e) => setNoteLevel(e.target.value as ScenarioNoteLevel)}
              className="rounded bg-tg-bg px-1 py-1 text-[11px]"
            >
              <option value="info">info</option>
              <option value="warn">warn</option>
              <option value="error">error</option>
            </select>
            <button
              type="button"
              disabled={busy !== null || !noteText.trim()}
              onClick={() => void submitNote()}
              className="shrink-0 rounded bg-tg-accent px-2 py-1 text-[11px] font-medium text-white disabled:opacity-50"
            >
              Add
            </button>
          </div>
          <div className="flex gap-1.5">
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => run("pass", () => endScenario(activeScenario.id, { status: "passed" }))}
              className="flex-1 rounded bg-tg-green/20 px-2 py-1 text-[11px] font-medium text-tg-green hover:bg-tg-green/30 disabled:opacity-50"
              title="Alt+P"
            >
              Passed <span className="opacity-60">(Alt+P)</span>
            </button>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => run("fail", () => endScenario(activeScenario.id, { status: "failed" }))}
              className="flex-1 rounded bg-tg-red/20 px-2 py-1 text-[11px] font-medium text-tg-red hover:bg-tg-red/30 disabled:opacity-50"
              title="Alt+F"
            >
              Failed <span className="opacity-60">(Alt+F)</span>
            </button>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => run("pause", () => deactivateScenario())}
              className="shrink-0 rounded bg-tg-hover px-2 py-1 text-[11px] text-tg-muted hover:text-tg-text disabled:opacity-50"
              title="Stop tagging new traffic without ending the scenario — resume later from its detail view."
            >
              Pause
            </button>
          </div>
        </div>
      ) : (
        <p className="rounded-md bg-tg-hover/40 px-2 py-1.5 text-[11px] text-tg-muted">
          Not recording — new messages and API calls land in the untagged bucket.
        </p>
      )}

      {!starting ? (
        <button
          type="button"
          onClick={() => setStarting(true)}
          className="w-full rounded bg-tg-hover px-2 py-1.5 text-left text-xs font-medium hover:bg-tg-hover/70"
        >
          + Start a scenario
        </button>
      ) : (
        <div className="space-y-1.5 rounded-md bg-tg-hover/60 p-2">
          {activeScenario && (
            <p className="text-[10px] text-tg-amber">
              Replaces &quot;{activeScenario.name}&quot; as the active scenario — its own tag stays on what it already recorded.
            </p>
          )}
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Name (what are you checking?)"
            className="w-full rounded bg-tg-bg px-2 py-1 text-xs placeholder:text-tg-muted"
            autoFocus
          />
          <input
            value={newDescription}
            onChange={(e) => setNewDescription(e.target.value)}
            placeholder="Description (optional)"
            className="w-full rounded bg-tg-bg px-2 py-1 text-xs placeholder:text-tg-muted"
          />
          <select
            value={newFeature || (featureLens !== ALL_FEATURES ? featureLens : "")}
            onChange={(e) => setNewFeature(e.target.value)}
            className="w-full rounded bg-tg-bg px-2 py-1 text-xs"
          >
            <option value="">No feature — this scenario rolls up to nothing</option>
            {features.map((feature) => (
              <option key={feature.id} value={feature.id}>
                {feature.title}
              </option>
            ))}
          </select>
          <input
            value={newTags}
            onChange={(e) => setNewTags(e.target.value)}
            placeholder="Tags, comma separated (optional)"
            className="w-full rounded bg-tg-bg px-2 py-1 text-xs placeholder:text-tg-muted"
          />
          <div className="flex gap-1.5">
            <button
              type="button"
              disabled={busy !== null || !newName.trim()}
              onClick={() => void submitStart()}
              className="flex-1 rounded bg-tg-accent px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
            >
              Start
            </button>
            <button type="button" onClick={() => setStarting(false)} className="rounded bg-tg-hover px-2 py-1 text-xs">
              Cancel
            </button>
          </div>
        </div>
      )}

      <div className="border-t border-tg-divider pt-1.5">
        <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-tg-muted">
          Filter to{featureLens !== ALL_FEATURES && <span className="normal-case"> (within the selected feature)</span>}
        </p>
        <select
          value={lens}
          onChange={(e) => onLensChange(e.target.value)}
          className="w-full rounded bg-tg-hover px-2 py-1 text-[11px]"
        >
          <option value={ALL_SCENARIOS}>All scenarios</option>
          <option value={UNTAGGED_SCENARIO}>Untagged traffic</option>
          {newestFirst.map((scenario) => (
            <option key={scenario.id} value={scenario.id}>
              {scenario.name} — {scenario.status} ({scenario.message_count}m/{scenario.api_call_count}c)
            </option>
          ))}
        </select>
      </div>

      {selected && (
        <ScenarioDetail
          scenario={selected}
          features={features}
          isActive={selected.id === activeScenarioId}
          onRefresh={onRefresh}
          onError={onError}
        />
      )}
    </div>
  );
}
