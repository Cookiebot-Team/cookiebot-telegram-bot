"""Captcha challenge generation and verification (core_groupguardian).

Pure CPU, Cython-compiled. Rendering the image is a worker job — this module only
does the maths and the constant-time comparison.

v1 stored live challenges in a flat `Captcha.txt` rewritten in full on every join
and every solve, guarded by a "sleep 1s if the file looks busy" helper that is not
a lock (`GroupShield.py:250-345`, `universal_funcs.py:331-338`). v2 keeps
challenges in `captcha_challenges` (distributed by group_id) with a TTL.
"""

from __future__ import annotations

import hmac
import secrets

import cython

# Not in HOT_MODULES: measured at ~11 us/challenge, essentially all of it inside
# `secrets.randbelow` (a CSPRNG syscall). Cython cannot make that faster, so per
# the benchmark gate this module ships pure Python. COMPILED stays exported so
# the gate can report on it.
COMPILED: bool = cython.compiled

# fmt: off
EMOJI_SET: tuple[str, ...] = (
    "🍪", "🐺", "🦊", "🐾", "🎲", "🍕", "🚀", "🎧", "🌙", "⭐", "🔥", "🍩",
)
# fmt: on


class Challenge:
    __slots__ = ("answer", "kind", "nonce", "options", "prompt")

    def __init__(self, kind: str, prompt: str, answer: str, options: list[str], nonce: str) -> None:
        self.kind: str = kind
        self.prompt: str = prompt
        self.answer: str = answer
        self.options: list[str] = options
        self.nonce: str = nonce


def make_arithmetic(max_operand: int = 9) -> Challenge:
    """`a + b = ?` with 4 plausible options. Cheap, language-neutral, screenreader-safe."""
    a: int = secrets.randbelow(max_operand) + 1
    b: int = secrets.randbelow(max_operand) + 1
    total: int = a + b
    answer: str = str(total)

    options: list[str] = [answer]
    while len(options) < 4:
        delta: int = secrets.randbelow(7) - 3
        if delta == 0:
            continue
        cand: str = str(total + delta)
        if cand not in options and total + delta > 0:
            options.append(cand)
    _shuffle(options)
    return Challenge("arithmetic", f"{a} + {b} = ?", answer, options, secrets.token_urlsafe(12))


def make_emoji(choices: int = 4) -> Challenge:
    """`tap the 🍪` — one tap, no typing, works on mobile."""
    if choices < 2 or choices > len(EMOJI_SET):
        raise ValueError("choices out of range")
    pool: list[str] = list(EMOJI_SET)
    _shuffle(pool)
    options: list[str] = pool[:choices]
    answer: str = options[secrets.randbelow(choices)]
    return Challenge("emoji", f"Tap the {answer}", answer, options, secrets.token_urlsafe(12))


def verify(challenge_answer: str, submitted: str) -> bool:
    """Constant-time compare so a solver cannot time-probe the answer."""
    if not challenge_answer or not submitted:
        return False
    return hmac.compare_digest(challenge_answer, submitted)


def callback_payload(nonce: str, option: str) -> str:
    """Telegram callback_data is capped at 64 bytes — keep it short and opaque."""
    return "cap:" + nonce + ":" + option


def parse_callback(data: str) -> tuple[str, str]:
    """Returns (nonce, option); ("", "") when the payload is not ours."""
    if not data.startswith("cap:"):
        return ("", "")
    rest: str = data[4:]
    sep: int = rest.find(":")
    if sep < 0:
        return ("", "")
    return (rest[:sep], rest[sep + 1 :])


def _shuffle(items: list[str]) -> None:
    """Fisher-Yates using the CSPRNG; `random.shuffle` is not acceptable here."""
    i: int = len(items) - 1
    while i > 0:
        j: int = secrets.randbelow(i + 1)
        items[i], items[j] = items[j], items[i]
        i -= 1
