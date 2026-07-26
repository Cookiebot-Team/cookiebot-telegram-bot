"use client";

// The three-pane shell: chat list, conversation, sandbox controls — Telegram
// Desktop's own layout. State lives here and flows down:
//
//   useSandbox()        -> snapshot + live events, refreshed on every SSE tick
//                           and a short poll underneath it (see useSandbox.ts)
//   currentUserId       -> which sandbox user the tester is "acting as" —
//                           the ONE place this choice lives; every control
//                           that used to keep its own copy now reads this
//   chatId              -> which chat is open in the centre pane
//   replyTo             -> the message the composer will reply to, if any
//   lastActionAt/-Label -> when the tester last did something that could
//                           provoke the bot, and what — drives both the
//                           waiting indicator and the API log's "+340ms"
//                           relative timestamps
//   activeScenario      -> what preset (if any) is currently loaded, so
//                           "what am I testing" survives scrolling the
//                           member list
//   lens                -> which feature, and which scenario within it, every
//                           pane is filtered to (see lib/lens.ts) — the axis
//                           validation actually happens along
//
// ChatList/UserSwitcher change currentUserId; MessageList/MessageBubble read
// it to decide bubble alignment and BOT tags; Composer/CommandPalette/
// MembershipPanel call back up to send a message, press a button, or change
// membership, then trigger a refresh.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import SandboxSidebar from "@/components/sandbox/SandboxSidebar";
import type { ScenarioOutcome } from "@/components/sandbox/ScenarioPanel";
import { confirmAndReset } from "@/components/sandbox/ScenarioPanel";
import {
  ALL_FEATURES,
  ALL_SCENARIOS,
  NO_LENS,
  UNFILED_FEATURE,
  UNTAGGED_SCENARIO,
  featureLabel,
  matchesLens,
  type Lens,
} from "@/lib/lens";
import ChatList from "@/components/chat/ChatList";
import MessageList from "@/components/chat/MessageList";
import Composer, { type ComposerSubmission } from "@/components/chat/Composer";
import { useSandbox } from "@/lib/useSandbox";
import { endScenario, pressCallback, sendMessage, type SendMessageParams } from "@/lib/api";
import { displayName, formatDuration } from "@/lib/format";
import type { SandboxMessage } from "@/types";

const ACTING_USER_KEY = "cookiebot-sandbox:actingUserId";

function readStoredUserId(): number | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(ACTING_USER_KEY);
  const parsed = raw ? Number(raw) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
}

function callbackAnswerText(payload: Record<string, unknown>): string | null {
  const text = payload.text;
  return typeof text === "string" && text.length > 0 ? text : null;
}

/** "Did the bot answer, and how long did it take" — separate from the SSE
 * `connected` dot, which only says the sandbox itself is reachable. Three
 * states: waiting (just acted, still within normal latency), silent (acted
 * a while ago, nothing back yet — a valid outcome, not a hang), answered
 * (at least one Bot API call landed after the action). */
function useBotActivity(lastActionAt: number | null, apiCallTimes: number[]) {
  const [, forceTick] = useState(0);

  const answeredAt = useMemo(() => {
    if (lastActionAt === null) return null;
    let latest: number | null = null;
    for (const at of apiCallTimes) {
      if (at >= lastActionAt && (latest === null || at > latest)) latest = at;
    }
    return latest;
  }, [lastActionAt, apiCallTimes]);

  useEffect(() => {
    if (lastActionAt === null || answeredAt !== null) return;
    const id = setInterval(() => forceTick((t) => t + 1), 200);
    return () => clearInterval(id);
  }, [lastActionAt, answeredAt]);

  if (lastActionAt === null) return null;
  if (answeredAt !== null) {
    return { status: "answered" as const, text: `Bot answered in ${formatDuration(lastActionAt, answeredAt)}` };
  }
  const elapsedS = Date.now() / 1000 - lastActionAt;
  const silent = elapsedS > 2;
  return {
    status: silent ? ("silent" as const) : ("waiting" as const),
    text: silent
      ? `No reply yet — ${elapsedS.toFixed(1)}s (silence can be the correct answer)`
      : `Waiting for the bot… ${elapsedS.toFixed(1)}s`,
  };
}

export default function Page() {
  const { snapshot, kit, events, connected, loading, error, refresh } = useSandbox();
  const [currentUserId, setCurrentUserIdState] = useState<number | null>(null);
  const [chatId, setChatId] = useState<number | null>(null);
  const [replyTo, setReplyTo] = useState<SandboxMessage | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [lastActionAt, setLastActionAt] = useState<number | null>(null);
  const [lastActionLabel, setLastActionLabel] = useState<string | null>(null);
  const [activeScenario, setActiveScenario] = useState<{
    label: string;
    whatToDo: string;
    whatToLookFor: string;
  } | null>(null);
  // What the workbench is looking at: a feature (every check that touched one
  // behaviour) and, within it, optionally one scenario. Unfiltered by default.
  // Both axes live in one object so no pane can ever apply half of it — see
  // `lib/lens.ts`.
  const [lens, setLens] = useState<Lens>(NO_LENS);
  const lastSendRef = useRef<{ chatId: number; params: SendMessageParams } | null>(null);

  // Read the persisted acting user once on mount; every subsequent selection
  // (from anywhere — ChatList's dropdown, the sidebar's chips, a preset)
  // goes through `selectUser`, which is the only writer.
  useEffect(() => {
    const stored = readStoredUserId();
    if (stored !== null) setCurrentUserIdState(stored);
  }, []);

  const selectUser = useCallback((id: number) => {
    setCurrentUserIdState(id);
    window.localStorage.setItem(ACTING_USER_KEY, String(id));
  }, []);

  const onAction = useCallback((label: string) => {
    setLastActionAt(Date.now() / 1000);
    setLastActionLabel(label);
  }, []);

  // Keep the acting user / open chat pointed at something real: pick a
  // default once data first arrives, and re-pick if a `reset` (or a leave)
  // made the current selection stop existing.
  useEffect(() => {
    if (!snapshot) return;
    const userStillExists = currentUserId !== null && snapshot.users.some((user) => user.id === currentUserId);
    if (!userStillExists) {
      const human = snapshot.users.find((user) => !user.is_bot);
      if (human) selectUser(human.id);
    }
    const chatStillExists = chatId !== null && snapshot.chats.some((chat) => chat.id === chatId);
    if (!chatStillExists) {
      setChatId(snapshot.chats.length > 0 ? snapshot.chats[0].id : null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reacts to snapshot arriving; re-checks existing selections each time
  }, [snapshot]);

  // Follow the lens to where its traffic actually is.
  //
  // A scenario's messages live in whatever chat it used, and a test suite
  // typically gives every test its own group — so filtering to a feature or a
  // test while some other chat is open shows an empty timeline, which reads
  // exactly like "the bot did nothing" for a check that in fact passed.
  // Switching to the chat holding the most of the selected traffic is what
  // the person asking for it meant. Only ever moves when the open chat has
  // none of it, so it cannot yank the view away mid-read.
  useEffect(() => {
    if (!snapshot) return;
    if (lens.feature === ALL_FEATURES && lens.scenario === ALL_SCENARIOS) return;
    const matches = (m: SandboxMessage) => matchesLens(m.scenario_id, lens, snapshot.scenarios);
    const inCurrent = chatId !== null && (snapshot.messages[String(chatId)] ?? []).some(matches);
    if (inCurrent) return;
    let best: { id: number; count: number } | null = null;
    for (const [id, chatMessages] of Object.entries(snapshot.messages)) {
      const count = chatMessages.filter(matches).length;
      if (count > 0 && (best === null || count > best.count)) best = { id: Number(id), count };
    }
    if (best) setChatId(best.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deliberately not keyed on chatId: this re-points the view when the lens changes, not every time the user picks a chat
  }, [snapshot, lens]);

  // Surface the bot's callback-query answers (the toast Telegram shows after
  // an inline button press) as a brief, self-dismissing banner.
  useEffect(() => {
    const last = events[events.length - 1];
    if (!last || last.kind !== "callback_answer") return;
    const text = callbackAnswerText(last.payload);
    if (!text) return;
    setToast(text);
    const timer = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(timer);
  }, [events]);

  const chat = useMemo(() => snapshot?.chats.find((c) => c.id === chatId) ?? null, [snapshot, chatId]);
  const messages = useMemo(() => (chatId !== null ? (snapshot?.messages[String(chatId)] ?? []) : []), [snapshot, chatId]);
  const scenarios = snapshot?.scenarios ?? [];
  const features = snapshot?.features ?? [];
  const visibleMessages = useMemo(
    () => messages.filter((m) => matchesLens(m.scenario_id, lens, scenarios)),
    [messages, lens, scenarios],
  );
  const filteredScenario =
    lens.scenario !== ALL_SCENARIOS && lens.scenario !== UNTAGGED_SCENARIO
      ? scenarios.find((s) => s.id === lens.scenario)
      : undefined;
  // What the unmissable filter banner below the chat header reads — `null`
  // means "not filtered", the one state that gets no banner at all. Both axes
  // are named, because "filtered to the captcha" and "filtered to one captcha
  // test" hide very different amounts of traffic and a tester must be able to
  // tell which one is in effect at a glance.
  const filterLabel = useMemo(() => {
    const parts: string[] = [];
    if (lens.feature === UNFILED_FEATURE) parts.push("scenarios filed under no feature");
    else if (lens.feature !== ALL_FEATURES) parts.push(featureLabel(features, lens.feature));
    if (lens.scenario === UNTAGGED_SCENARIO) parts.push("untagged traffic");
    else if (lens.scenario !== ALL_SCENARIOS)
      parts.push(filteredScenario?.name ?? lens.scenario);
    return parts.length > 0 ? parts.join(" › ") : null;
  }, [lens, features, filteredScenario]);
  const currentUser = useMemo(
    () => snapshot?.users.find((user) => user.id === currentUserId) ?? null,
    [snapshot, currentUserId],
  );
  const currentMembership = useMemo(
    () => chat?.members.find((m) => m.user_id === currentUserId) ?? null,
    [chat, currentUserId],
  );
  const replyToSender = useMemo(
    () => (replyTo ? snapshot?.users.find((user) => user.id === replyTo.from_id) : undefined),
    [snapshot, replyTo],
  );
  const apiCallTimes = useMemo(() => snapshot?.api_calls.map((c) => c.at) ?? [], [snapshot]);
  const activity = useBotActivity(lastActionAt, apiCallTimes);

  const doSend = useCallback(
    async (targetChatId: number, params: SendMessageParams, actionLabel: string) => {
      onAction(actionLabel);
      lastSendRef.current = { chatId: targetChatId, params: { ...params, reply_to_message_id: undefined } };
      await sendMessage(targetChatId, params);
      await refresh();
    },
    [onAction, refresh],
  );

  async function handleSend(submission: ComposerSubmission) {
    if (currentUserId === null || chatId === null) return;
    const replyId = replyTo?.message_id ?? undefined;
    setReplyTo(null);
    const base: SendMessageParams = {
      user_id: currentUserId,
      text: submission.text,
      media: submission.media,
      media_file_id: submission.mediaFileId,
      media_caption: submission.mediaCaption,
      anonymous: submission.anonymous,
      reply_to_message_id: replyId,
    };
    const label = submission.media ? `send ${submission.media}` : `send "${submission.text ?? ""}"`;
    for (let i = 0; i < submission.repeat; i += 1) {
      await doSend(chatId, base, submission.repeat > 1 ? `${label} (${i + 1}/${submission.repeat})` : label);
    }
  }

  async function handleSendText(text: string) {
    if (currentUserId === null || chatId === null) return;
    await doSend(chatId, { user_id: currentUserId, text }, text);
  }

  async function repeatLastSend() {
    const last = lastSendRef.current;
    if (!last) return;
    await doSend(last.chatId, last.params, `repeat: ${last.params.text ?? last.params.media ?? "message"}`);
  }

  async function handlePressButton(message: SandboxMessage, data: string) {
    if (currentUserId === null) return;
    onAction(`press "${data}"`);
    await pressCallback(message.chat_id, { user_id: currentUserId, message_id: message.message_id, data });
    await refresh();
  }

  function applyPreset(outcome: ScenarioOutcome) {
    setActiveScenario({ label: outcome.label, whatToDo: outcome.whatToDo, whatToLookFor: outcome.whatToLookFor });
    if (outcome.actingUserId !== null) selectUser(outcome.actingUserId);
    setChatId(outcome.chatId);
    setReplyTo(null);
    // A preset seeds a fresh world, so any scenario the lens was pointed at
    // no longer has traffic in it. Point the lens at the preset's own feature
    // instead: the palette below narrows to it, and whatever the tester is
    // about to provoke lands in a view that is already about the right thing.
    setLens({ feature: outcome.featureId ?? ALL_FEATURES, scenario: ALL_SCENARIOS });
    onAction(`scenario: ${outcome.label}`);
    void refresh();
  }

  // Global, keyboard-driven controls: Alt+1..9 switches the acting user
  // (matching the index shown on each chip's tooltip in UserSwitcher), Alt+.
  // resends the last thing sent (the one-key half of "make repeat-send
  // cheap"), Alt+R resets with the same confirmation the sidebar button uses.
  //
  // Alt+0 clears the filter — both axes, feature and scenario — the same
  // escape hatch Alt+1..9 gives the user switcher. Alt+N/Alt+P/Alt+F are the scenario
  // controls a manual tester reaches for mid-flow (note / mark passed / mark
  // failed); they always act on `active_scenario_id`, never on whatever the
  // filter below happens to be showing, so a keystroke can't land on the
  // wrong scenario. Alt+N focuses `ScenarioRail`'s note input (a shortcut
  // can supply no text of its own) rather than submitting one.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!event.altKey) return;
      const humans = snapshot?.users.filter((u) => !u.is_bot) ?? [];
      if (/^[1-9]$/.test(event.key)) {
        const index = Number(event.key) - 1;
        if (humans[index]) {
          event.preventDefault();
          selectUser(humans[index].id);
        }
        return;
      }
      if (event.key === "0") {
        event.preventDefault();
        setLens(NO_LENS);
        return;
      }
      if (event.key === ".") {
        event.preventDefault();
        void repeatLastSend();
        return;
      }
      if (event.key.toLowerCase() === "r") {
        event.preventDefault();
        void confirmAndReset(applyPreset);
        return;
      }
      if (event.key.toLowerCase() === "n") {
        event.preventDefault();
        document.getElementById("scenario-note-input")?.focus();
        return;
      }
      // Deliberately skips `onAction`: that feeds the "did the bot answer
      // yet" indicator, and ending a scenario never provokes a bot reply —
      // calling it here would leave that indicator stuck on "waiting"
      // forever afterwards.
      const activeId = snapshot?.active_scenario_id ?? null;
      if (activeId === null) return;
      if (event.key.toLowerCase() === "p") {
        event.preventDefault();
        void endScenario(activeId, { status: "passed" }).then(refresh);
      } else if (event.key.toLowerCase() === "f") {
        event.preventDefault();
        void endScenario(activeId, { status: "failed" }).then(refresh);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reads current snapshot/refs at fire time, doesn't need to resubscribe on every change
  }, [snapshot]);

  const composerDisabled = currentUserId === null || chatId === null;

  return (
    <div className="flex h-full w-full overflow-hidden bg-tg-bg-secondary text-tg-text">
      <div className="w-[320px] shrink-0 border-r border-tg-divider">
        <ChatList
          snapshot={snapshot}
          currentUserId={currentUserId}
          chatId={chatId}
          onSelectChat={setChatId}
          onSelectUser={selectUser}
        />
      </div>

      <div className="relative flex min-w-0 flex-1 flex-col bg-tg-bg">
        {toast && (
          <div className="pointer-events-none absolute left-1/2 top-3 z-10 -translate-x-1/2 rounded-full bg-tg-panel px-4 py-1.5 text-[13px] shadow-lg">
            {toast}
          </div>
        )}

        {chat ? (
          <>
            <div className="flex shrink-0 flex-col border-b border-tg-divider bg-tg-panel">
              <div className="flex items-center justify-between px-4 py-2.5">
                <div>
                  <div className="text-[15px] font-semibold">{chat.title}</div>
                  <div className="text-[12px] text-tg-muted">
                    {chat.members.length} member{chat.members.length === 1 ? "" : "s"}
                    {currentUser && (
                      <>
                        {" "}
                        · acting as {displayName(currentUser)}
                        {currentMembership?.anonymous && <span className="text-tg-amber"> (anonymous admin)</span>}
                      </>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-1.5 text-[12px] text-tg-muted">
                  <span className={`h-2 w-2 rounded-full ${connected ? "bg-tg-green" : "bg-tg-red"}`} />
                  {connected ? "live" : "reconnecting…"}
                </div>
              </div>

              {/* The scenario filter's own banner — deliberately louder and
                  positioned above the preset "Testing: …" banner below it, and
                  never combined with it into one line: a tester scanning past
                  the preset instructions must not be able to miss that half
                  the traffic in this view is being hidden by a filter. */}
              {filterLabel && (
                <div className="flex items-center gap-2 border-t border-tg-divider bg-tg-amber/15 px-4 py-1.5 text-[12px]">
                  <span className="font-semibold text-tg-amber">
                    Filtered to scenario: {filterLabel}
                    {filteredScenario && ` (${filteredScenario.status})`}.
                  </span>
                  <span className="text-tg-muted">Other traffic in this chat is hidden.</span>
                  <button
                    type="button"
                    onClick={() => setLens(NO_LENS)}
                    className="ml-auto shrink-0 rounded bg-tg-amber/25 px-2 py-0.5 text-[11px] font-medium text-tg-amber hover:bg-tg-amber/35"
                  >
                    Clear filter <span className="opacity-70">(Alt+0)</span>
                  </button>
                </div>
              )}

              {activeScenario && (
                <div className="border-t border-tg-divider bg-tg-hover/40 px-4 py-1.5 text-[12px]">
                  <span className="font-semibold text-tg-accent">Testing: {activeScenario.label}.</span>{" "}
                  <span className="text-tg-text/90">{activeScenario.whatToDo}</span>{" "}
                  <span className="text-tg-muted">Look for: {activeScenario.whatToLookFor}</span>
                </div>
              )}

              {activity && (
                <div
                  className={`border-t border-tg-divider px-4 py-1 text-[11px] ${
                    activity.status === "answered"
                      ? "text-tg-green"
                      : activity.status === "silent"
                        ? "text-tg-amber"
                        : "text-tg-muted"
                  }`}
                >
                  {activity.status === "waiting" && (
                    <span className="mr-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-tg-muted align-middle" />
                  )}
                  {activity.text}
                  {lastActionLabel && <span className="text-tg-muted"> — after {lastActionLabel}</span>}
                </div>
              )}
            </div>

            <MessageList
              chat={chat}
              messages={visibleMessages}
              events={events}
              users={snapshot?.users ?? []}
              bot={snapshot?.bot ?? null}
              currentUserId={currentUserId}
              scenarios={scenarios}
              showScenarioTags={lens.scenario === ALL_SCENARIOS}
              onReply={setReplyTo}
              onPressButton={handlePressButton}
            />

            <Composer
              disabled={composerDisabled}
              replyTo={replyTo}
              replyToSender={replyToSender}
              canSendAnonymously={currentMembership?.anonymous === true}
              onCancelReply={() => setReplyTo(null)}
              onSend={handleSend}
            />
          </>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center text-tg-muted">
            {loading ? (
              <p>Loading the sandbox…</p>
            ) : error ? (
              <>
                <p className="text-tg-red">Could not reach the sandbox server.</p>
                <p className="text-[13px]">{error}</p>
              </>
            ) : (
              <>
                <p className="text-[15px] font-medium text-tg-text">No chats yet</p>
                <p className="text-[13px]">Seed a scenario from the sidebar to create a group and some users.</p>
              </>
            )}
          </div>
        )}
      </div>

      <div className="w-[360px] shrink-0 border-l border-tg-divider">
        {snapshot ? (
          <SandboxSidebar
            snapshot={snapshot}
            currentUserId={currentUserId}
            chatId={chatId}
            lastActionAt={lastActionAt}
            kit={kit}
            lens={lens}
            onLensChange={setLens}
            onSelectUser={selectUser}
            onRefresh={refresh}
            onApplyPreset={applyPreset}
            onAction={onAction}
            onSendText={handleSendText}
          />
        ) : (
          <div className="flex h-full items-center justify-center px-4 text-center text-[13px] text-tg-muted">
            {error ? "Could not reach the sandbox server." : "Loading sandbox controls…"}
          </div>
        )}
      </div>
    </div>
  );
}
