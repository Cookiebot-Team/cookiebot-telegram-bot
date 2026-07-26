// Typed client for the sandbox control API. Every route is proxied through
// Next.js (`next.config.ts` rewrites `/api/*` to the sandbox server), so all
// paths here are relative and same-origin — no CORS, no base URL to thread
// through props.
//
// This is the one client: every request/response shape below is transcribed
// directly from `packages/cb-sandbox/src/cb_sandbox/control_api.py`'s pydantic
// models, not guessed. If the server disagrees, `bunx tsc --noEmit` and the
// live send will both fail loudly — that mismatch is exactly what happened
// before this file existed (see git history: `sendMessage` posted `from_id`
// where the server wants `user_id`, every send 422'd). Every UI component
// goes through the named exports here, never a raw `fetch`, so the contract
// only needs fixing in one place.

import type {
  ApiCall,
  ChatType,
  Feature,
  SendMediaKind,
  Membership,
  Role,
  SandboxChat,
  SandboxFile,
  SandboxKit,
  SandboxMessage,
  SandboxSnapshot,
  SandboxUser,
  Scenario,
  ScenarioNoteLevel,
  ScenarioStatus,
  SeedFixture,
} from "@/types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      `${init?.method ?? "GET"} ${path} failed: ${response.status}${detail ? ` — ${detail}` : ""}`,
    );
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text.length > 0 ? (JSON.parse(text) as T) : (undefined as T));
}

// `body` is only ever handed to `JSON.stringify`, never read back — typing it
// `unknown` (rather than a `Record<string, unknown>` index signature) lets
// every call site pass its own named params interface without a structural
// mismatch, while `request<T>`'s return type still keeps callers type-safe.
function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

// Only the scenario patch route uses PATCH; everything else that mutates
// state is POST, so this doesn't earn its own file, just its own helper.
function patch<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: "PATCH", body: JSON.stringify(body) });
}

/** `GET /api/state` — the whole sandbox world. */
export function getState(): Promise<SandboxSnapshot> {
  return request<SandboxSnapshot>("/api/state");
}

/** `GET /api/kit` — what the bot under test *is*: identity, seeds, presets,
 * commands, features. Static for the server's lifetime, so fetch it once on
 * mount. This is what keeps the client free of any one bot's specifics. */
export function getKit(): Promise<SandboxKit> {
  return request<SandboxKit>("/api/kit");
}

/** `GET /api/features` — the feature rollup on its own. The same list rides
 * along on every snapshot; this exists for a caller that wants only the
 * validation view (a CI check, a report) without the whole world. */
export function getFeatures(): Promise<Feature[]> {
  return request<Feature[]>("/api/features");
}

/** `POST /api/reset` — wipe the world and reseed the configured default
 * (`SandboxConfig.default_seed`, whatever this bot's config calls it). */
export function reset(): Promise<SandboxSnapshot> {
  return post<SandboxSnapshot>("/api/reset");
}

/** `POST /api/seed` — wipe the world and load a named fixture. Omit the name
 * to get the configured default. */
export function seed(scenario?: SeedFixture): Promise<SandboxSnapshot> {
  return post<SandboxSnapshot>("/api/seed", scenario ? { scenario } : {});
}

// --------------------------------------------------------------------- scenarios
//
// A `Scenario` is a named span of activity, not a fixture (see `SeedFixture`
// above) — every message and API call made while one is active carries its
// id, which is what lets the workbench filter "just this check" out of a
// sandbox holding a whole run's worth of traffic.

export interface CreateScenarioParams {
  /** Lets a caller that already has its own id for this run (an e2e test's
   * nodeid, say) use it as the scenario id directly instead of getting one
   * minted and having to thread it back. */
  id?: string;
  name: string;
  description?: string;
  source?: string;
  /** Which feature this scenario checks — the axis the workbench groups a
   * whole run by. Optional; the server falls back to matching `tags`. */
  feature?: string;
  tags?: string[];
  metadata?: Record<string, unknown>;
  /** Server defaults this to `true` — starting a scenario almost always
   * means "tag everything from now on", not "log it for later". */
  activate?: boolean;
}

/** `POST /api/scenarios` -> `ScenarioOut`, 201. */
export function createScenario(params: CreateScenarioParams): Promise<Scenario> {
  return post<Scenario>("/api/scenarios", params);
}

/** `POST /api/scenarios/{id}/activate` -> `ScenarioOut`. Makes this the one
 * new traffic gets tagged with — including a scenario that was previously
 * deactivated (paused) rather than ended, to resume tagging on it. */
export function activateScenario(id: string): Promise<Scenario> {
  return post<Scenario>(`/api/scenarios/${id}/activate`);
}

/** `POST /api/scenarios/deactivate` — stop tagging new traffic without
 * ending the active scenario, so it can be resumed later via `activateScenario`. */
export function deactivateScenario(): Promise<{ active_scenario_id: null }> {
  return post<{ active_scenario_id: null }>("/api/scenarios/deactivate");
}

export interface AddScenarioNoteParams {
  text: string;
  level?: ScenarioNoteLevel;
}

/** `POST /api/scenarios/{id}/notes` -> `ScenarioOut`. The manual half of a
 * scenario's timeline — what a human tester was checking and noticed, next
 * to the bot's own recorded behaviour rather than in a separate notebook. */
export function addScenarioNote(id: string, params: AddScenarioNoteParams): Promise<Scenario> {
  return post<Scenario>(`/api/scenarios/${id}/notes`, params);
}

export interface PatchScenarioParams {
  status?: ScenarioStatus;
  description?: string;
  feature?: string;
  tags?: string[];
  metadata?: Record<string, unknown>;
}

/** `PATCH /api/scenarios/{id}` -> `ScenarioOut`. Not symmetric on the two
 * collection fields: `metadata` merges server-side, `tags` replaces
 * wholesale — mirrored here, not guessed. */
export function patchScenario(id: string, params: PatchScenarioParams): Promise<Scenario> {
  return patch<Scenario>(`/api/scenarios/${id}`, params);
}

export interface EndScenarioParams {
  status?: ScenarioStatus;
}

/** `POST /api/scenarios/{id}/end` -> `ScenarioOut`. How a manual tester
 * records their verdict — passed or failed — on the scenario they were
 * driving by hand. */
export function endScenario(id: string, params?: EndScenarioParams): Promise<Scenario> {
  return post<Scenario>(`/api/scenarios/${id}/end`, params);
}

export interface UploadFileParams {
  filename?: string;
  content_type?: string;
  /** A whole `data:` URL or a bare base64 payload — the server accepts both,
   * so a `FileReader.readAsDataURL` result can be passed straight through. */
  data: string;
  duration?: number;
}

/** `POST /api/files` -> `FileOut`, 201. Content-addressed: uploading the same
 * picture twice returns the same id rather than a second copy. */
export function uploadFile(params: UploadFileParams): Promise<SandboxFile> {
  return post<SandboxFile>("/api/files", params);
}

/** Where the bytes for a stored file live. Not a fetch — this is the URL an
 * `<img>`/`<video>`/`<audio>` element points at directly, which is what makes
 * a photo in the timeline an actual photo. */
export function fileUrl(fileId: string): string {
  return `/api/files/${encodeURIComponent(fileId)}`;
}

export interface CreateUserParams {
  first_name: string;
  username: string;
  language_code?: string;
}

/** `POST /api/users` -> `UserOut` */
export function createUser(params: CreateUserParams): Promise<SandboxUser> {
  return post<SandboxUser>("/api/users", params);
}

export interface CreateChatParams {
  title: string;
  /** Defaults to `"supergroup"` server-side. Pass `"private"` to test a
   * command gated on `chat.type === "private"`. This chat's id is minted from
   * the same counter every chat gets, so the bot cannot reach it — use
   * `openDm` for a DM the bot can actually answer in. */
  type?: ChatType;
}

/** `POST /api/chats` -> `ChatOut` */
export function createChat(params: CreateChatParams): Promise<SandboxChat> {
  return post<SandboxChat>("/api/chats", params);
}

/** `POST /api/users/{userId}/dm` -> `ChatOut`.
 *
 * "This user presses Start". The DM's id is the user's own id, which is the
 * only id a handler answering privately (`bot.send_message(user_id, ...)`) ever
 * has — without it, the bot's every private reply comes back `403 Forbidden:
 * bot can't initiate conversation with a user`, exactly as on real Telegram.
 * Idempotent. */
export function openDm(userId: number): Promise<SandboxChat> {
  return post<SandboxChat>(`/api/users/${userId}/dm`);
}

export interface JoinChatParams {
  user_id: number;
  /** Omitted = self-join. Present = added by another member — the fork the
   * doomlist and captcha branch on (`JoinRequest.by_user_id` in control_api.py). */
  by_user_id?: number;
}

/** `POST /api/chats/{chatId}/join` -> `ChatOut` */
export function joinChat(chatId: number, params: JoinChatParams): Promise<SandboxChat> {
  return post<SandboxChat>(`/api/chats/${chatId}/join`, params);
}

export interface LeaveChatParams {
  user_id: number;
  /** Omitted = the user left on their own (Telegram "left"). Present = they
   * were removed by this actor (Telegram "kicked"). */
  by_user_id?: number;
}

/** `POST /api/chats/{chatId}/leave` -> `ChatOut` */
export function leaveChat(chatId: number, params: LeaveChatParams): Promise<SandboxChat> {
  return post<SandboxChat>(`/api/chats/${chatId}/leave`, params);
}

export interface PatchMemberParams {
  role?: Role;
  anonymous?: boolean;
}

/** `POST /api/chats/{chatId}/members/{userId}` -> `ChatOut`. Change an
 * existing member's role or anonymity toggle without a leave+rejoin. */
export function patchMember(
  chatId: number,
  userId: number,
  patch: PatchMemberParams,
): Promise<SandboxChat> {
  return post<SandboxChat>(`/api/chats/${chatId}/members/${userId}`, patch);
}

export interface SendMessageParams {
  user_id: number;
  text?: string;
  reply_to_message_id?: number;
  media?: SendMediaKind;
  /** A `file_id` from `uploadFile` — the real bytes behind the media. Omit to
   * send a media message with no contents, which is what a flood test wants. */
  media_file_id?: string;
  media_caption?: string;
  /** Requires the sender's membership to already have `anonymous` toggled on
   * via `patchMember` — sending anonymously doesn't itself turn anonymity on,
   * exactly as Telegram models it (`SendMessageRequest.anonymous` in
   * control_api.py). */
  anonymous?: boolean;
}

/** `POST /api/chats/{chatId}/messages` -> `MessageOut`. Post a message into a
 * chat as the given sandbox user — this is what queues the update
 * cb-gateway's polling ingest picks up. */
export function sendMessage(chatId: number, params: SendMessageParams): Promise<SandboxMessage> {
  return post<SandboxMessage>(`/api/chats/${chatId}/messages`, params);
}

export interface PressCallbackParams {
  user_id: number;
  message_id: number;
  data: string;
}

/** `POST /api/chats/{chatId}/callback` — simulate pressing one inline
 * keyboard button. Returns the raw queued Telegram `Update`, which nothing in
 * the UI currently reads; typed `unknown` rather than invented since it's not
 * part of the `web/types.ts` contract. */
export function pressCallback(chatId: number, params: PressCallbackParams): Promise<unknown> {
  return post(`/api/chats/${chatId}/callback`, params);
}

// Re-exported so components that only need the button shape don't have to
// reach into `@/types` themselves just for this one type.
export type { ApiCall, Feature, Membership, SandboxFile, SandboxKit, SandboxUser };
