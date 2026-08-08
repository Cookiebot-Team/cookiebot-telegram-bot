"""In-process mock of the Telegram Bot API.

Lets the full handler stack — parser, filters, middlewares, aiogram session —
run in CI with no live token and no network. Records every outbound call so
assertions can be made on what the bot actually sent.

Speaks the real URL shape (`/bot<token>/<method>`), so aiogram is not aware it is
talking to a mock; only `CB_TELEGRAM_API_BASE` changes.
"""

from __future__ import annotations

import json
import time
from typing import Any

from aiohttp import web


def _chat_member(user_id: int, role: str, *, is_anonymous: bool = False) -> dict[str, Any]:
    """A ChatMember payload aiogram will actually parse.

    `ChatMemberOwner` needs only `user` and `is_anonymous`; `ChatMemberAdministrator`
    additionally requires all eleven `can_*` flags plus `can_be_edited`.
    """
    user = {
        "id": user_id,
        "is_bot": False,
        "first_name": f"Admin{user_id}",
        "username": f"admin{user_id}",
    }
    if role == "creator":
        return {"status": "creator", "user": user, "is_anonymous": is_anonymous}
    return {
        "status": "administrator",
        "user": user,
        "is_anonymous": is_anonymous,
        "can_be_edited": False,
        "can_manage_chat": True,
        "can_delete_messages": True,
        "can_manage_video_chats": True,
        "can_restrict_members": True,
        "can_promote_members": False,
        "can_change_info": True,
        "can_invite_users": True,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
    }


class MockTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port: int = 0
        self._message_id = 1000
        # chat_id -> getChatAdministrators result. Scenarios set this; nothing is
        # an admin by default, so a handler that forgets its admin check fails
        # the test rather than passing by accident.
        self.admins: dict[int, list[dict[str, Any]]] = {}
        # method -> Telegram error description. Lets a scenario say "cas.chat is
        # down" or "the bot lost its admin rights" without patching our own code.
        self.failures: dict[str, str] = {}
        # chat_id -> getChatMemberCount result. util_everyone's `known = min(len
        # (usernames), get_chat_member_count(...))` (design R4.6) needs a real
        # int back, not the mock's generic `{}` fallback (which aiogram cannot
        # parse as a `getChatMemberCount` response and raises). Defaults large so
        # a scenario that never calls `set_member_count` is never clamped by it.
        self.member_counts: dict[int, int] = {}
        # user_id -> list of file_ids, most-recent-photo-first, largest-size-last
        # per entry -- fun_battle's `get_user_profile_photos(user_id, limit=1)
        # .photos[0][-1].file_id` (docs/contracts/fun_battle.md) needs a real
        # file_id back, not the mock's generic `{}` fallback. Absent from this
        # dict means "no profile photo" (an empty `photos` list), matching
        # Telegram's own response for a user with none set or a private one.
        self.profile_photos: dict[int, list[str]] = {}

    def set_member_count(self, chat_id: int, count: int) -> None:
        self.member_counts[chat_id] = count

    def set_profile_photo(self, user_id: int, file_id: str) -> None:
        self.profile_photos[user_id] = [file_id]

    def set_admins(
        self, chat_id: int, admins: list[tuple[int, str]], *, is_anonymous: bool = False
    ) -> None:
        """`admins` is (user_id, role) with role in {creator, administrator}.

        Every field `ChatMemberAdministrator` marks required is emitted, because
        aiogram validates the response and a partial payload does not fail loudly:
        the parse error surfaces as an empty admin list, so an admin-gated handler
        keeps working — it just decides nobody is an admin, and the test that was
        meant to prove an admin can act passes for the wrong reason.
        """
        self.admins[chat_id] = [
            _chat_member(user_id, role, is_anonymous=is_anonymous) for user_id, role in admins
        ]

    def fail(self, method: str, description: str = "Bad Request: test failure") -> None:
        self.failures[method] = description

    def clear_failures(self) -> None:
        self.failures.clear()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    def reset(self) -> None:
        self.calls.clear()
        # Per-scenario, like `calls` and like the admin sets `qa/conftest.py`
        # clears by hand. Left alone, a scenario that gave someone a profile
        # photo leaks it into the next one's "and now someone without a photo
        # tries it", which then passes while asserting the opposite of what it
        # reads — the same trap `_reset_scenario_state`'s docstring describes.
        # Every suite that needs a photo sets it in a per-scenario step.
        self.profile_photos.clear()
        self.member_counts.clear()

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/bot{token}/{method}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        # aiohttp assigns the port lazily; read it back off the bound socket.
        server = self._site._server  # noqa: SLF001
        self.port = server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        try:
            payload = dict(await request.post())
        except Exception:  # noqa: BLE001 - some methods send an empty body
            payload = {}
        self.calls.append((method, payload))
        if method in self.failures:
            return web.json_response(
                {"ok": False, "error_code": 400, "description": self.failures[method]},
                status=400,
            )
        return web.json_response({"ok": True, "result": self._result(method, payload)})

    def _result(
        self, method: str, payload: dict[str, Any]
    ) -> dict[str, Any] | list[dict[str, Any]] | bool | int:
        if method == "getMe":
            return {
                "id": 424242,
                "is_bot": True,
                "first_name": "Cookiebot",
                "username": "CookieMWbot",
            }
        if method == "getChatAdministrators":
            return self.admins.get(int(payload.get("chat_id", 0)), [])
        if method == "getChatMember":
            chat_id = int(payload.get("chat_id", 0))
            user_id = int(payload.get("user_id", 0))
            for admin in self.admins.get(chat_id, []):
                if admin["user"]["id"] == user_id:
                    return admin
            return {
                "status": "member",
                "user": {"id": user_id, "is_bot": False, "first_name": "Member"},
            }
        if method == "getChatMemberCount":
            return self.member_counts.get(int(payload.get("chat_id", 0)), 1_000_000)
        if method == "getChat":
            return {
                "id": int(payload.get("chat_id", -100)),
                "type": "supergroup",
                "title": "QA Group",
            }
        if method == "getUserProfilePhotos":
            file_ids = self.profile_photos.get(int(payload.get("user_id", 0)), [])
            # One size variant per photo is enough: fun_battle only ever reads
            # `photos[0][-1]` (the largest of the most recent), so the mock does
            # not need to model Telegram's real multi-size list.
            photos = [
                [{"file_id": file_id, "file_unique_id": file_id, "width": 512, "height": 512}]
                for file_id in file_ids
            ]
            return {"total_count": len(photos), "photos": photos}
        if method == "sendMediaGroup":
            # `SendMediaGroup.__returning__` is `list[Message]`, unlike every
            # other send* method here -- a single dict fails aiogram's response
            # validation instead of a plain wrong-shape assertion downstream.
            media_count = len(json.loads(payload.get("media", "[]")))
            self._message_id += 1
            return [
                {
                    "message_id": self._message_id + offset,
                    "date": int(time.time()),
                    "chat": {"id": int(payload.get("chat_id", -100)), "type": "supergroup"},
                    "from": {
                        "id": 424242,
                        "is_bot": True,
                        "first_name": "Cookiebot",
                        "username": "CookieMWbot",
                    },
                }
                for offset in range(max(media_count, 1))
            ]
        if method in {
            "sendMessage",
            "sendPhoto",
            "sendAnimation",
            "sendSticker",
            "sendVideo",
            "sendVoice",
            "sendAudio",
            "sendDocument",
            "sendPoll",
            "editMessageText",
            "editMessageReplyMarkup",
        }:
            self._message_id += 1
            return {
                "message_id": self._message_id,
                "date": int(time.time()),
                "chat": {"id": int(payload.get("chat_id", -100)), "type": "supergroup"},
                "from": {
                    "id": 424242,
                    "is_bot": True,
                    "first_name": "Cookiebot",
                    "username": "CookieMWbot",
                },
                "text": payload.get("text", ""),
            }
        if method in {
            "setWebhook",
            "deleteWebhook",
            "answerCallbackQuery",
            "setMyCommands",
            "sendChatAction",
            "deleteMessage",
            "restrictChatMember",
            "banChatMember",
            "unbanChatMember",
            "promoteChatMember",
            "pinChatMessage",
            "leaveChat",
        }:
            return True
        return {}
