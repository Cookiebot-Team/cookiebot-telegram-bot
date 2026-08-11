"""Command parsing and link classification — hot path, Cython-compiled.

Replaces v1's dispatcher, which was a ~250-line `if/elif` chain over raw
`msg['text']` (`COOKIEBOT.py:185-316`) re-evaluated for every message: worst case
it ran dozens of substring comparisons before falling through.
Here parsing is one pass plus a dict lookup.
"""

from __future__ import annotations

import re

import cython

COMPILED: bool = cython.compiled


# Alias -> canonical command. Canonical names match the QA specs
# (Cookiebot-QA/features/*.feature); v1's PT/ES aliases are preserved as inputs.
# fmt: off
COMMAND_ALIASES: dict[str, str] = {
    # Accented spellings are separate v1 triggers, not decoration: the dispatcher
    # lists "/aleatório", "/aniversário", "/cumpleaños", "/reclamação" and
    # "/rojão" alongside their unaccented forms, and a Portuguese or Spanish
    # keyboard produces the accented one by default. ("/gênero" is also a v1
    # trigger; its feature is not ported yet, so it has no canonical name here.)
    # core
    "commands": "commands", "comandos": "commands",
    "privacy": "privacy", "privacidade": "privacy", "privacidad": "privacy",
    # Spanish aliases were missing here: v1 dispatches all three spellings of each
    # of these at COOKIEBOT.py:264-268, so a Spanish group's commands would have
    # stopped working on v2. Dropping an alias is the one thing §2.1 forbids.
    "rules": "rules", "regras": "rules", "reglas": "rules",
    "newrules": "newrules", "novasregras": "newrules", "nuevasreglas": "newrules",
    "newwelcome": "newwelcome", "novobemvindo": "newwelcome",
    "nuevabienvenida": "newwelcome",
    "isalive": "isalive", "tavivo": "isalive",
    # QA spells this /config; v1 shipped /configurar (FEATURE-MAP mismatch) — accept both.
    "config": "config", "configure": "config", "configurar": "config",
    # fun
    "dice": "dice", "dado": "dice", "roll": "dice",
    "ship": "ship", "shipp": "ship", "shippar": "ship",
    "death": "death", "morte": "death", "muerte": "death",
    "meme": "meme",
    "battle": "battle", "batalha": "battle", "batalla": "battle",
    "random": "random", "aleatorio": "random", "aleatório": "random",
    "firecracker": "firecracker", "rojao": "firecracker", "rojão": "firecracker",
    "acende": "firecracker", "fogos": "firecracker",
    "complaint": "complaint", "milton": "complaint", "reclamacao": "complaint",
    "reclamação": "complaint", "queja": "complaint",
    # x_distortion. v1 dispatches all three spellings (COOKIEBOT.py:217,242).
    "destroy": "destroy", "zoar": "destroy", "destruir": "destroy",
    # util
    "birthday": "birthday", "aniversario": "birthday", "aniversário": "birthday",
    "cumpleanos": "birthday", "cumpleaños": "birthday",
    "nextbirthday": "nextbirthday", "nextbirthdays": "nextbirthday",
    "proximosaniversarios": "nextbirthday", "proximoscumpleanos": "nextbirthday",
    "everyone": "everyone",
    "adm": "calladms", "admin": "calladms", "report": "calladms",
    "youtube": "youtube",
    # x_analysis. All three spellings are v1 triggers (COOKIEBOT.py:202).
    "analysis": "analysis", "analise": "analysis", "analisis": "analysis",
    # x_unearth. Two spellings, both v1 triggers (COOKIEBOT.py:236).
    "unearth": "unearth", "desenterrar": "unearth",
    # x_giveaways. v1 has exactly one spelling (COOKIEBOT.py:249,262) — no
    # PT/ES alias was ever shipped, so inventing one here would be a new
    # trigger, not a preserved one.
    "giveaway": "giveaway",
    "transcribe": "transcribe", "transcrever": "transcribe", "transcribir": "transcribe",
    # QA spells this /deletereposts; v1 shipped /deleteposts — accept both.
    "deletereposts": "deletereposts", "deleteposts": "deletereposts",
    "apagarposts": "deletereposts",
    "publish": "publish", "divulgar": "publish", "publicar": "publish",
    "repost": "repost", "repostar": "repost", "reenviar": "repost",
    # x_reverse_search. v1 dispatches all three (COOKIEBOT.py:212); the
    # Portuguese spelling is the one its own users type.
    "searchsource": "searchsource", "buscarfonte": "searchsource",
    "buscarfuente": "searchsource",
    # x_owner_commands. v1 dispatches /grupos and /groups (COOKIEBOT.py:83)
    # and the rest in one spelling each (:97-105). Owner-gated and
    # private-chat only, so these never collide with a group command.
    "grupos": "groups", "groups": "groups",
    "leave": "leave",
    "blacklist": "blacklist", "unblacklist": "unblacklist",
    "broadcast": "broadcast",
    "stop": "stop", "restart": "restart",
    # partnered conventions (fun_partneredcons) — /trex was spec'd but missing in v1.
    "bff": "con_bff", "patas": "con_patas", "fursmeet": "con_fursmeet",
    "trex": "con_trex", "furcamp": "con_furcamp", "pawstral": "con_pawstral",
}
# fmt: on

# /d20, /d6 — dice shorthand that carries its own argument.
#
# No digit cap: v1 parses the sides with a bare `int(text.split()[0][2:])`
# (`Miscellaneous.py:172`), so `/d99999` rolls. A 4-digit cap here meant such a
# command did not parse as a command at all and the bot answered nothing —
# silence, not an error. Bounds are the handler's business: `fun_dice` replies
# with v1's usage text for values it will not roll.
_DICE_SHORTHAND = re.compile(r"^d(\d+)$")

# Social links the embedder rewrites (util_embedder).
_EMBEDDABLE = re.compile(
    r"https?://(?:www\.)?"
    r"(?P<host>x\.com|twitter\.com|bsky\.app|instagram\.com|tiktok\.com|reddit\.com|"
    r"pixiv\.net|e621\.net|furaffinity\.net)/\S+",
    re.IGNORECASE,
)


@cython.cclass
class ParsedCommand:
    # `visibility="public"` is required: cdef-class attributes are private to C by
    # default, and handlers read these from Python.
    name = cython.declare(str, visibility="public")
    args = cython.declare(str, visibility="public")
    target_bot = cython.declare(str, visibility="public")
    raw = cython.declare(str, visibility="public")

    def __init__(self, name: str, args: str, target_bot: str, raw: str) -> None:
        self.name = name
        self.args = args
        self.target_bot = target_bot
        self.raw = raw

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ParsedCommand(name={self.name!r}, args={self.args!r}, target_bot={self.target_bot!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ParsedCommand):
            return NotImplemented
        return (
            self.name == other.name
            and self.args == other.args
            and self.target_bot == other.target_bot
        )


@cython.ccall
def parse_command(text: str, bot_username: str = "") -> ParsedCommand | None:
    """Parse a leading /command[@bot] [args].

    Returns None for non-commands and for commands explicitly addressed at a
    different bot (Telegram delivers those to every bot in the group).
    """
    if not text:
        return None
    if text[0] != "/":
        return None

    end: cython.Py_ssize_t = len(text)
    i: cython.Py_ssize_t = 1
    while i < end and not text[i].isspace():
        i += 1
    head: str = text[1:i]
    args: str = text[i:].strip()
    if not head:
        return None

    target: str = ""
    at: cython.Py_ssize_t = head.find("@")
    if at >= 0:
        target = head[at + 1 :]
        head = head[:at]

    if target and bot_username and target.lower() != bot_username.lower():
        return None

    key: str = head.lower()
    canonical: str = COMMAND_ALIASES.get(key, "")
    if not canonical:
        m = _DICE_SHORTHAND.match(key)
        if m is not None:
            sides: str = m.group(1)
            return ParsedCommand("dice", sides if not args else sides + " " + args, target, text)
        return None
    return ParsedCommand(canonical, args, target, text)


def find_embeddable_links(text: str) -> list[str]:
    """Social links worth rewriting into an embeddable form (util_embedder)."""
    if not text or "http" not in text:
        return []
    return [m.group(0) for m in _EMBEDDABLE.finditer(text)]


def mentions_bot(text: str, names: tuple[str, ...]) -> bool:
    """Cheap case-insensitive name check for the conversational-AI trigger."""
    if not text:
        return False
    lowered: str = text.lower()
    # Explicit loop, not any(): the generator would build a Python frame per call
    # in the compiled build. This runs on every message.
    for name in names:  # noqa: SIM110
        if name in lowered:
            return True
    return False


def normalise_username(username: str) -> str:
    """Strip a leading @ and lowercase — usernames are matched case-insensitively."""
    if not username:
        return ""
    return username[1:].lower() if username[0] == "@" else username.lower()
