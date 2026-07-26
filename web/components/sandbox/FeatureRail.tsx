"use client";

// The validation view: one row per feature the bot claims to have, showing
// how many scenarios exercised it and how they ended.
//
// This is the pane that answers the question a per-test result list cannot.
// A test report tells you which of 200 checks failed; it cannot tell you
// whether the captcha, as a behaviour, is still correct — that is a question
// about one feature and every scenario that touched it, and the two facts
// needed to answer it (what the bot claims to do, what this run actually
// checked) live in different places until here.
//
// The row that matters most is the one with a zero. A feature with no
// scenarios looks exactly like a passing feature in every per-test report
// ever written; here it reads `untested`, in the same list, sorted near the
// top. Everything else in this tool shows what happened; this shows what
// didn't.
//
// Clicking a feature filters the whole workbench to it (see `lib/lens.ts`)
// and lists its scenarios, so "why did this fail" is one more click and not a
// context switch.

import { useMemo, useState } from "react";
import type { Feature, Scenario } from "@/types";
import {
  ALL_FEATURES,
  featureVerdict,
  scenariosInFeature,
  sortFeaturesForReview,
  UNFILED_FEATURE,
  VERDICT_STYLES,
  type FeatureLens,
  type ScenarioLens,
  ALL_SCENARIOS,
} from "@/lib/lens";
import { formatElapsed } from "@/lib/format";

const STATUS_DOT: Record<string, string> = {
  done: "bg-tg-green",
  partial: "bg-tg-amber",
  planned: "bg-tg-muted",
  blocked: "bg-tg-red",
};

function VerdictPill({ feature }: { feature: Feature }) {
  const style = VERDICT_STYLES[featureVerdict(feature)];
  return (
    <span
      className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ${style.className}`}
    >
      {style.label}
    </span>
  );
}

/** `3/4 passed · 1 failed` — the counts behind the verdict, in the callers'
 * own vocabulary. Not normalised to a fixed set: a suite that reports
 * "flaky" should see "flaky" rather than have it folded into something this
 * file invented. */
function StatusCounts({ feature }: { feature: Feature }) {
  const entries = Object.entries(feature.status_counts);
  if (entries.length === 0) {
    return <span className="text-tg-muted">no scenarios have exercised this yet</span>;
  }
  return (
    <>
      {entries
        .sort((a, b) => b[1] - a[1])
        .map(([status, count], index) => (
          <span key={status}>
            {index > 0 && <span className="text-tg-muted"> · </span>}
            <span
              className={
                status === "failed"
                  ? "text-tg-red"
                  : status === "passed"
                    ? "text-tg-green"
                    : "text-tg-muted"
              }
            >
              {count} {status}
            </span>
          </span>
        ))}
    </>
  );
}

function ScenarioLine(props: {
  scenario: Scenario;
  selected: boolean;
  onSelect: (id: ScenarioLens) => void;
}) {
  const { scenario, selected, onSelect } = props;
  const failed = scenario.status === "failed";
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(selected ? ALL_SCENARIOS : scenario.id)}
        title={scenario.description ?? scenario.name}
        className={`flex w-full items-baseline gap-1.5 rounded px-1 py-0.5 text-left text-[11px] hover:bg-tg-hover/60 ${
          selected ? "bg-tg-hover" : ""
        }`}
      >
        <span
          className={`h-1.5 w-1.5 shrink-0 rounded-full ${
            failed
              ? "bg-tg-red"
              : scenario.status === "passed"
                ? "bg-tg-green"
                : scenario.status === "running"
                  ? "bg-tg-accent"
                  : "bg-tg-muted"
          }`}
        />
        <span className={`min-w-0 flex-1 truncate ${failed ? "text-tg-red" : ""}`}>
          {scenario.name}
        </span>
        <span className="shrink-0 text-tg-muted">
          {formatElapsed(scenario.started_at, scenario.ended_at ?? Date.now() / 1000)}
        </span>
      </button>
    </li>
  );
}

export default function FeatureRail(props: {
  features: Feature[];
  scenarios: Scenario[];
  featureLens: FeatureLens;
  scenarioLens: ScenarioLens;
  onFeatureChange: (lens: FeatureLens) => void;
  onScenarioChange: (lens: ScenarioLens) => void;
}) {
  const { features, scenarios, featureLens, scenarioLens, onFeatureChange, onScenarioChange } =
    props;
  const [query, setQuery] = useState("");
  const [onlyProblems, setOnlyProblems] = useState(false);

  const unfiled = useMemo(() => scenarios.filter((s) => s.feature === null), [scenarios]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    let rows = sortFeaturesForReview(features);
    if (q) {
      rows = rows.filter(
        (f) =>
          f.title.toLowerCase().includes(q) ||
          f.id.toLowerCase().includes(q) ||
          f.commands.some((c) => c.toLowerCase().includes(q)),
      );
    }
    if (onlyProblems) {
      // "Problems" deliberately includes `untested`: a feature nobody checked
      // is not a pass, and this toggle exists to produce the shortlist a
      // person works through before signing off on a release.
      rows = rows.filter((f) => ["failed", "mixed", "untested"].includes(featureVerdict(f)));
    }
    return rows;
  }, [features, query, onlyProblems]);

  const totals = useMemo(() => {
    const counts = { failed: 0, untested: 0, passed: 0 };
    for (const feature of features) {
      const verdict = featureVerdict(feature);
      if (verdict === "failed") counts.failed += 1;
      else if (verdict === "untested") counts.untested += 1;
      else if (verdict === "passed") counts.passed += 1;
    }
    return counts;
  }, [features]);

  if (features.length === 0) {
    return (
      <p className="rounded-md bg-tg-hover/40 px-2 py-1.5 text-[11px] text-tg-muted">
        No features declared. Add a <code>features</code> list to the sandbox config
        (<code>sandbox.config.json</code>) to group a run by what the bot is supposed to do.
      </p>
    );
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2 text-[11px]">
        <span className="text-tg-green">{totals.passed} passed</span>
        <span className="text-tg-red">{totals.failed} failed</span>
        <span className="text-tg-muted">{totals.untested} untested</span>
        <button
          type="button"
          onClick={() => setOnlyProblems((v) => !v)}
          className={`ml-auto shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${
            onlyProblems ? "bg-tg-amber/30 text-tg-amber" : "bg-tg-hover text-tg-muted"
          }`}
          title="Failed, mixed, and never-exercised features — the shortlist before a release."
        >
          Needs attention
        </button>
      </div>

      <div className="flex gap-1.5">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter features…"
          className="min-w-0 flex-1 rounded bg-tg-hover px-2 py-1 text-[11px] placeholder:text-tg-muted"
        />
        {featureLens !== ALL_FEATURES && (
          <button
            type="button"
            onClick={() => {
              onFeatureChange(ALL_FEATURES);
              onScenarioChange(ALL_SCENARIOS);
            }}
            className="shrink-0 rounded bg-tg-hover px-2 py-1 text-[10px] text-tg-muted hover:text-tg-text"
          >
            Clear
          </button>
        )}
      </div>

      <ul className="max-h-96 space-y-0.5 overflow-y-auto">
        {visible.map((feature) => {
          const selected = featureLens === feature.id;
          const mine = selected ? scenariosInFeature(scenarios, feature.id) : [];
          return (
            <li key={feature.id} className="rounded bg-tg-hover/30">
              <button
                type="button"
                onClick={() => {
                  onFeatureChange(selected ? ALL_FEATURES : feature.id);
                  // Drop any scenario drill-down when the feature changes —
                  // leaving it would filter to a scenario that is not in the
                  // newly selected feature, showing an empty timeline that
                  // reads exactly like "the bot did nothing".
                  onScenarioChange(ALL_SCENARIOS);
                }}
                className={`flex w-full items-center gap-1.5 px-1.5 py-1 text-left ${
                  selected ? "bg-tg-hover" : ""
                }`}
              >
                <span
                  className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                    STATUS_DOT[feature.status] ?? "bg-tg-muted"
                  }`}
                  title={`declared status: ${feature.status}`}
                />
                <span className="min-w-0 flex-1 truncate text-[12px]">{feature.title}</span>
                <span className="shrink-0 text-[10px] text-tg-muted">
                  {feature.scenario_count}
                </span>
                <VerdictPill feature={feature} />
              </button>

              {selected && (
                <div className="space-y-1 px-1.5 pb-1.5">
                  {feature.description && (
                    <p className="text-[11px] text-tg-text/80">{feature.description}</p>
                  )}
                  {feature.commands.length > 0 && (
                    <p className="font-mono text-[10px] text-tg-muted">
                      {feature.commands.join("  ")}
                    </p>
                  )}
                  <p className="text-[11px]">
                    <StatusCounts feature={feature} />
                  </p>
                  {mine.length > 0 && (
                    <ul className="space-y-0.5 border-t border-tg-divider pt-1">
                      {mine.map((scenario) => (
                        <ScenarioLine
                          key={scenario.id}
                          scenario={scenario}
                          selected={scenarioLens === scenario.id}
                          onSelect={onScenarioChange}
                        />
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </li>
          );
        })}
        {visible.length === 0 && (
          <li className="px-1 py-1 text-[11px] text-tg-muted">No feature matches that filter.</li>
        )}
      </ul>

      {unfiled.length > 0 && (
        <button
          type="button"
          onClick={() => {
            onFeatureChange(featureLens === UNFILED_FEATURE ? ALL_FEATURES : UNFILED_FEATURE);
            onScenarioChange(ALL_SCENARIOS);
          }}
          className={`w-full rounded px-1.5 py-1 text-left text-[11px] ${
            featureLens === UNFILED_FEATURE ? "bg-tg-hover" : "bg-tg-hover/30 text-tg-muted"
          }`}
          title="Scenarios that name no feature — work that exists but rolls up to nothing."
        >
          Unfiled scenarios <span className="text-tg-muted">({unfiled.length})</span>
        </button>
      )}
    </div>
  );
}
