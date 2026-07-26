"""util_embedder — rewrite social links into an embeddable form.

v1: `Bot/SocialContent.py:79-84` `check_reply_embed`, called unconditionally
for every ordinary group text message that reaches the dispatcher's trailing
`else` (`Bot/COOKIEBOT.py:309-312`) — i.e. only once nothing earlier in the
big `if/elif` chain claimed the message (not a command, not a reply to the
welcome/rules/captcha/milton prompts, not the "who" trigger, not the
conversational-AI trigger), and only `if utilityfunctions:` (v1's
`functionsUtility` gate, `COOKIEBOT.py:311`). Private chats never reach this
code at all (`COOKIEBOT.py:106-110`'s early return).

`check_reply_embed` itself only gates on `'link_preview_options' in msg` (a
key Telegram includes only on text messages that carry a link) before calling
`fix_embed_if_social_link`, which:

  1. Refuses to touch a message that already contains a fixed-domain host
     (`vxtwitter.com`, `fxtwitter.com`, `fixupx.com`, `d.tnktok.com`,
     `vm.vxtiktok.com`, `ddinstagram.com`, `kkinstagram.com`, `fxbsky.app`) —
     avoids double-rewriting an already-embeddable link.
  2. Calls `requests.get(message, timeout=2)` on the *entire message text* to
     "validate" it — a real v1 defect, not a deliberate design: `requests.get`
     raises on anything that is not a bare, complete URL, so in practice this
     feature only ever fired when the whole message was nothing but the link.
     A link embedded in a sentence ("check this out https://x.com/...") never
     triggered v1's rewrite at all, silently. `find_embeddable_links`
     (`cb_core/textmatch.py`) is already built and tested to find a link
     anywhere in the text, including several in one message
     (`packages/cb-core/tests/test_hot_modules.py::TestLinks`) — this port
     follows that established contract rather than the network-validation
     defect, which is also consistent with AGENTS.md's "nothing slow on the
     reply path" rule (a synchronous `requests.get` per message is exactly the
     kind of blocking call that rule forbids). See docs/contracts/util_embedder.md
     for the full parity discussion.
  3. For a TikTok short link (`vm.tiktok.com/...`, `tiktok.com/t/...`) only,
     resolves the redirect with a second `requests.get` to get the long-form
     URL before extracting from it. `_EMBEDDABLE` in `cb_core/textmatch.py`
     only matches literal `tiktok.com/...` (and `www.tiktok.com/...`), never
     the `vm.tiktok.com` subdomain, so this port cannot detect a short link at
     all — a known gap, not a silent behaviour change, since nothing this
     handler owns can add a host to that regex (out of file ownership; see the
     contract doc).
  4. Rewrites exactly three hosts, in this priority order, first match wins:
     Twitter/X status links -> `fixupx.com`, TikTok long-form video links ->
     `vm.vxtiktok.com`, Bluesky profile/post links -> `fxbsky.app`. A fourth
     transformation, Instagram -> `kkinstagram.com`, is present in the v1
     source but commented out (`SocialContent.py:60,71-74`) — i.e. **v1 today
     does not rewrite Instagram links**, and this port matches that, not the
     dead code.
  5. Replies (never edits, never deletes the original) with just the
     rewritten URL, `link_preview_options={'show_above_text': True,
     'prefer_large_media': True, 'disable_web_page_preview': False}`
     (`SocialContent.py:84`).

`find_embeddable_links` also recognises `reddit.com`, `pixiv.net`, `e621.net`
and `furaffinity.net` as "embeddable" hosts — none of which v1 ever rewrites
(they appear nowhere in `SocialContent.py`). That module is compiled and
already unit-tested (not owned by this port), so its host list is used as-is
for *detection*; this handler only ever produces a rewritten link for the
three hosts v1 actually maps to a fixed domain, and silently ignores any
other detected link, exactly as v1 would (nothing rewrites them there either).

QA: `../Cookiebot-QA/features/util_embedder.feature` ->
`qa/features/util_embedder.feature`. Contract: `docs/contracts/util_embedder.md`.
"""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import LinkPreviewOptions, Message

from cb_core.logging import get_logger
from cb_core.textmatch import find_embeddable_links
from cb_gateway.filters import FeatureGate

router = Router(name="embedder")

log = get_logger("cb.embedder")

# Priority order matches v1's `transformations` list (`SocialContent.py:57-61`):
# Twitter/X first, then TikTok, then Bluesky. Each pattern is anchored to the
# host so it only matches within a link `find_embeddable_links` already
# confirmed carries that host - it never re-derives "is this the right site".
_TWITTER_STATUS = re.compile(r"(?:twitter\.com|x\.com)/([^/?#]+/status/\d+)", re.IGNORECASE)
_TIKTOK_VIDEO = re.compile(r"tiktok\.com/(@[^/?#]+/video/\d+)", re.IGNORECASE)
_BSKY_PROFILE = re.compile(r"bsky\.app/profile/(.+)$", re.IGNORECASE)


def _rewrite_one(link: str) -> str | None:
    """A single detected link -> its fixed-domain embed, or `None`.

    `None` covers both "not one of the three hosts v1 actually rewrites"
    (Instagram, reddit, pixiv, e621, furaffinity - all detected by
    `find_embeddable_links` but never mapped to a target in v1) and "matched
    the host but not v1's stricter per-host shape" (e.g. a bare
    `tiktok.com/foo` with no `@user/video/id`, which v1's own extraction would
    also have failed to produce a rewrite for).
    """
    if match := _TWITTER_STATUS.search(link):
        return f"https://fixupx.com/{match.group(1)}"
    if match := _TIKTOK_VIDEO.search(link):
        return f"https://vm.vxtiktok.com/{match.group(1)}"
    if match := _BSKY_PROFILE.search(link):
        return f"https://fxbsky.app/profile/{match.group(1)}"
    return None


def rewritten_links(text: str) -> list[str]:
    """Every embeddable link in `text` that this handler knows how to rewrite.

    Order matches the order the links appear in the message - `find_embeddable_links`
    walks the text once with `re.finditer` and preserves that order.
    """
    rewritten = []
    for link in find_embeddable_links(text):
        target = _rewrite_one(link)
        # v1's own guard (`SocialContent.py:83`): never "rewrite" a link to
        # itself. Unreachable in practice given the target hosts always
        # differ from the source hosts, but cheap to keep explicit.
        if target and target != link:
            rewritten.append(target)
    return rewritten


@router.message(F.text, F.chat.type.in_({"group", "supergroup"}), FeatureGate("utility"))
async def rewrite_embeddable_links(message: Message) -> None:
    """Reply with the embeddable form of every social link this handler owns.

    Every path that does not act raises `SkipHandler`, not a quiet return:
    this filter matches on every ordinary group text message with the utility
    area on, so other handlers over the same update (present and future) must
    still get their turn when this one has nothing to do.
    """
    text = message.text or ""
    # v1 never reaches `check_reply_embed` for a command: the whole
    # `msg['text'].startswith('/')` branch (`COOKIEBOT.py:186`) is a sibling
    # of the trailing `else` this feature lives in, not a parent of it, so a
    # message starting with "/" is skipped here whether or not it parses as a
    # known command (COMMAND_ALIASES is irrelevant to this decision).
    if text.startswith("/"):
        raise SkipHandler
    # v1's call site requires `'from' in msg` (`COOKIEBOT.py:310-312`) -
    # absent for messages posted through a linked channel with no sender.
    if message.from_user is None:
        raise SkipHandler

    targets = rewritten_links(text)
    if not targets:
        raise SkipHandler

    await message.reply(
        "\n".join(targets),
        link_preview_options=LinkPreviewOptions(
            show_above_text=True,
            prefer_large_media=True,
            is_disabled=False,
        ),
    )


__all__ = ["rewritten_links", "router"]
