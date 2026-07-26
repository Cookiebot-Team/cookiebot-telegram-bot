// Shapes shared by both halves of the sandbox — the client and
// `cb_sandbox/control_api.py`. Every interface below mirrors a pydantic model
// in that file field-for-field; `packages/cb-sandbox/tests/test_control_api.py`
// (`TestStateShape`) asserts the server side of the same contract, so a drift
// here is a fast, loud failure rather than a silent 422 in the browser.
//
// This file is not owned by chat/ or sandbox/ — both import from here so the
// panes agree on one truth.

export type Role = "creator" | "administrator" | "member" | "restricted" | "kicked" | "left";

export type ChatType = "private" | "group" | "supergroup";

/** control_api.py `MediaKind` — every kind a stored message can carry, including
 * the ones only the bot produces (`sendDocument`/`sendAudio`/`sendVoice`/`sendDice`). */
export type MediaKind =
  | "photo"
  | "sticker"
  | "video"
  | "animation"
  | "document"
  | "audio"
  | "voice"
  | "dice";

/** control_api.py `SendMediaKind` — the subset a human can attach in the
 * composer. `dice` is missing on purpose: real Telegram rolls it server-side. */
export type SendMediaKind = Exclude<MediaKind, "dice">;

/** control_api.py `SeedRequest.scenario` — the name of a starting *world*.
 *
 * A plain string, not a union: which worlds exist is a fact about the bot
 * under test (`sandbox.config.json`), not about this client. `GET /api/kit`
 * lists them — see `SandboxSeed`. Called `Scenario` in this file before the
 * server grew a real, per-run Scenario concept (see `Scenario` below, a
 * different thing: a named span of activity, not a fixture). */
export type SeedFixture = string;

/** control_api.py `UserOut` */
export interface SandboxUser {
  id: number;
  first_name: string;
  last_name: string | null;
  username: string;
  language_code: string;
  is_bot: boolean;
}

/** control_api.py `MembershipOut` */
export interface Membership {
  user_id: number;
  role: Role;
  anonymous: boolean;
  joined_at: number;
  /** Unix seconds; `0` means "not restricted", matching `restrictChatMember`'s
   * own `until_date=0` == "forever/not set" convention. */
  restricted_until: number;
}

/** control_api.py `ChatOut` */
export interface SandboxChat {
  id: number;
  title: string;
  type: ChatType;
  members: Membership[];
}

/** One Telegram `MessageEntity`. `offset`/`length` are UTF-16 code units,
 * which is exactly how JavaScript indexes a string — so they can be applied
 * with `slice()` directly, with no conversion. */
export interface MessageEntity {
  type: string;
  offset: number;
  length: number;
  url?: string;
  language?: string;
  custom_emoji_id?: string;
}

export interface InlineButton {
  text: string;
  callback_data?: string;
  url?: string;
}

/** control_api.py `MessageOut` */
export interface SandboxMessage {
  message_id: number;
  chat_id: number;
  from_id: number;
  text: string | null;
  date: number;
  sender_chat_id: number | null;
  reply_to_message_id: number | null;
  reply_markup: { inline_keyboard: InlineButton[][] } | null;
  media: MediaKind | null;
  /** Which stored file the media actually is — fetch it from
   * `/api/files/{id}`. `null` means the media has no bytes in this sandbox
   * (a `file_id` minted by production, a seeded fixture), which the client
   * draws as a labelled placeholder rather than a broken image. */
  media_file_id: string | null;
  media_caption: string | null;
  /** Formatting the bot asked for, parsed out of its `parse_mode` markup by the
   * sandbox exactly as real Telegram does — `text` and `media_caption` are the
   * plain strings. Render these or the tester sees unformatted text and cannot
   * tell a broken link from a working one. */
  entities: MessageEntity[];
  caption_entities: MessageEntity[];
  /** Set on a membership service message (a join or a leave), which carries no
   * text — Telegram models those as ordinary messages too, and the captcha
   * replies to one. `by_user_id` present on a leave means removed, not left. */
  service: { kind: string; user_id: number; by_user_id: number | null } | null;
  edited: boolean;
  deleted: boolean;
  /** Which `Scenario` was active when this was recorded, `null` if none was —
   * that's its own bucket ("untagged"), not a gap to hide. */
  scenario_id: string | null;
}

/** control_api.py `ApiCallOut` — one Bot API call the bot made. The "what did
 * the bot actually do" panel, and the tool's main validation surface. */
export interface ApiCall {
  method: string;
  payload: Record<string, unknown>;
  at: number;
  /** Same tag as `SandboxMessage.scenario_id` — which `Scenario` was active
   * when the bot made this call, `null` for untagged traffic. */
  scenario_id: string | null;
}

/** control_api.py `FileOut` — a stored blob's metadata. The bytes come from
 * `GET /api/files/{file_id}`, never inline, so a snapshot stays small no
 * matter how many pictures a run went through. */
export interface SandboxFile {
  file_id: string;
  file_unique_id: string;
  mime_type: string;
  file_name: string;
  size: number;
  width: number;
  height: number;
  duration: number;
}

/** control_api.py `NoteLevel` — a note's severity, so a tester's "got
 * silence, that's a bug" reads differently from "as expected". Unlike
 * `ScenarioStatus`, this one *is* a closed `Literal` server-side. */
export type ScenarioNoteLevel = "info" | "warn" | "error";

/** control_api.py `SandboxScenario.status` — a plain `str`, not a closed
 * union: the e2e suite and a human doing manual UAT are free to use whatever
 * word tells the next reader what happened, and `control_api.py` never gates
 * behaviour on the value. `"running"` (the default) and `"closed"` (what
 * `end_scenario` falls back to when no status is given) are the only two the
 * server itself ever writes; `"passed"`/`"failed"`/`"skipped"` are this
 * client's own convention for the manual pass/fail controls, not a server
 * guarantee — anything else that shows up here must still render, not crash. */
export type ScenarioStatus = string;

/** One entry in `ScenarioOut.notes` — the server types the list as
 * `list[dict[str, Any]]` (no dedicated pydantic model), but `add_scenario_note`
 * is its only writer and always produces exactly this shape. */
export interface ScenarioNote {
  at: number;
  text: string;
  level: ScenarioNoteLevel;
}

/** control_api.py `ScenarioOut` — a named span of activity. Every message and
 * API call made while this scenario is the active one carries its `id`
 * (see `SandboxMessage.scenario_id` / `ApiCall.scenario_id`), which is what
 * lets the workbench answer "which check produced this" after a run that
 * left behind hundreds of both. `metadata` is deliberately untyped: a test
 * runner attaches whatever it knows (nodeid, source file, expectations) and
 * the client has no way to predict the keys — render whatever is there. */
export interface Scenario {
  id: string;
  name: string;
  description: string | null;
  /** Free-form: "e2e" | "manual" | "preset" in practice, but the server
   * doesn't constrain it, so this stays a plain string. */
  source: string | null;
  /** Which `Feature` this scenario was checking — either what the caller set
   * or what the server inferred from `tags`. `null` means the scenario is
   * filed under no feature, which is its own bucket in the picker, not a gap
   * to hide: an unfiled scenario is work nobody can roll up. */
  feature: string | null;
  tags: string[];
  metadata: Record<string, unknown>;
  status: ScenarioStatus;
  notes: ScenarioNote[];
  started_at: number;
  ended_at: number | null;
  message_count: number;
  api_call_count: number;
}

// `control_api.py`'s `stream_events` documents the kinds it actually emits
// via `sandbox.publish(...)` call sites in that file and in `telegram_api.py`:
// "message" on every send, "member" on join/leave/patch/restrict/ban/promote,
// "reset" on wipe. "edit"/"delete"/"callback_answer"/"api_call" are reserved
// for calls the bot itself makes (editMessageText, deleteMessage,
// answerCallbackQuery, every recorded call) — not yet published as discrete
// SSE events, only visible via the `api_calls` list in a fresh snapshot.
export type SandboxEventKind =
  | "message"
  | "edit"
  | "delete"
  | "member"
  | "callback_answer"
  | "api_call"
  | "reset";

export interface SandboxEvent {
  kind: SandboxEventKind;
  payload: Record<string, unknown>;
  at: number;
}

/** control_api.py `SandboxSnapshot` — the whole world, as `GET /api/state`
 * and every mutating endpoint's response body return it. `messages` is keyed
 * by chat id; JSON object keys are always strings, so this is
 * `Record<string, ...>` even though the server's own type is `dict[int, ...]`. */
export interface SandboxSnapshot {
  users: SandboxUser[];
  chats: SandboxChat[];
  messages: Record<string, SandboxMessage[]>;
  api_calls: ApiCall[];
  bot: SandboxUser | null;
  /** Ordered by `started_at`, oldest first. */
  scenarios: Scenario[];
  /** The scenario every new message/API call is being tagged with right now,
   * or `null` if none is running — in which case new traffic lands in the
   * untagged bucket. */
  active_scenario_id: string | null;
  /** Every configured feature with this run's scenario counts folded in.
   * On the snapshot rather than fetched separately so the feature rollup can
   * never render one poll out of step with the scenario list beside it. */
  features: Feature[];
}

// ------------------------------------------------------------------- the kit
//
// `GET /api/kit` — what the bot under test *is*, served rather than compiled
// in, so this client works against any bot's sandbox with no rebuild. Every
// interface below mirrors a pydantic model in `control_api.py`.

/** Where a feature stands in the bot itself. `done`/`partial`/`planned`/
 * `blocked` get a distinct treatment; the server allows any string, so
 * anything else must still render rather than crash. */
export type FeatureStatus = string;

/** control_api.py `FeatureOut` — one configured feature, plus what this run
 * has to say about it. The counts are the point: a feature's metadata is
 * static, but "4 scenarios ran, 3 passed, 1 failed" is the run, and
 * validation happens where the two meet. */
export interface Feature {
  id: string;
  title: string;
  description: string | null;
  status: FeatureStatus;
  commands: string[];
  /** Scenario tags that also mean this feature — how a suite that labels its
   * runs gets grouped without setting `feature` explicitly. */
  tags: string[];
  docs: string | null;
  /** Scenario ids claimed by this feature, oldest first. */
  scenario_ids: string[];
  scenario_count: number;
  /** `{status: count}` in whatever vocabulary the callers used. Not a fixed
   * set — a suite reporting "flaky" should see "flaky". */
  status_counts: Record<string, number>;
  message_count: number;
  api_call_count: number;
}

/** control_api.py `SeedOut` — a starting world the picker can offer. */
export interface SandboxSeed {
  name: string;
  title: string;
  description: string;
  user_count: number;
  chat_count: number;
}

/** control_api.py `CommandOut` — one row in the command palette. */
export interface SandboxCommand {
  /** The `/word` a tester should actually type or click. */
  primary: string;
  /** Internal canonical name, e.g. `"calladms"`. Not always valid command
   * text itself — use `primary` for that. */
  canonical: string;
  /** Every other trigger that resolves to the same canonical command. */
  aliases: string[];
  feature_id: string | null;
  title: string | null;
  status: FeatureStatus;
  hint: string | null;
}

/** control_api.py `PresetOut` — one click that puts the tester in front of a
 * specific question. It states what to do and what to watch for; it does not
 * assert the outcome, because the bot's real reaction *is* the result. */
export interface SandboxPreset {
  id: string;
  button: string;
  label: string;
  seed: string;
  feature_id: string | null;
  /** A seed user's key (or username) to act as, if the preset names one. */
  acting_user: string | null;
  /** `{first_name, username_prefix}` — mint a fresh account and act as it,
   * for checks whose whole point is that the account has no history. */
  create_user: { first_name?: string; username_prefix?: string } | null;
  chat: string | null;
  what_to_do: string;
  what_to_look_for: string;
}

/** control_api.py `KitOut` — everything this client would otherwise hardcode. */
export interface SandboxKit {
  bot: { id: number; username: string; first_name: string };
  /** Which config file the server loaded, or "built-in defaults". Shown in
   * the UI: a palette that disagrees with the bot is almost always a stale
   * or unexpected config, and this names the file to fix. */
  config_source: string;
  default_seed: string;
  seeds: SandboxSeed[];
  presets: SandboxPreset[];
  commands: SandboxCommand[];
  features: Feature[];
}
