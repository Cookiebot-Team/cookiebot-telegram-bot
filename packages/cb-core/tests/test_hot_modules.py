"""Unit tests for the Cython-targeted modules.

These run against whichever build is present — compiled or pure Python — so a
compilation change can never alter behaviour without a test noticing.
"""

from __future__ import annotations

import pytest

from cb_core import captcha, cooldowns, dedupe, textmatch


class TestTokenBucket:
    def test_burst_then_refill(self) -> None:
        b = cooldowns.TokenBucket(capacity=3.0, rate=1.0, now=0.0)
        assert b.allow(0.0) and b.allow(0.0) and b.allow(0.0)
        assert not b.allow(0.0)
        assert b.allow(1.0)  # one token refilled after a second

    def test_never_exceeds_capacity(self) -> None:
        b = cooldowns.TokenBucket(capacity=2.0, rate=100.0, now=0.0)
        b.allow(0.0)
        b.allow(1000.0)
        assert b.tokens <= 2.0

    def test_retry_after(self) -> None:
        b = cooldowns.TokenBucket(capacity=1.0, rate=2.0, now=0.0)
        assert b.allow(0.0)
        assert b.retry_after() == pytest.approx(0.5)


class TestSlidingWindow:
    def test_exceeds_within_window(self) -> None:
        w = cooldowns.SlidingWindow(limit=3, window=10.0)
        for i in range(3):
            assert not w.exceeded(float(i))
        assert w.exceeded(3.0)

    def test_old_entries_fall_out(self) -> None:
        w = cooldowns.SlidingWindow(limit=2, window=5.0)
        w.hit(0.0)
        w.hit(1.0)
        assert not w.exceeded(100.0)  # window moved past both

    def test_flood_does_not_grow_unbounded(self) -> None:
        w = cooldowns.SlidingWindow(limit=2, window=1e9)
        for i in range(1000):
            w.hit(float(i))
        assert w.count <= 2 * 4 + 1


class TestQuotaLedger:
    def test_day_rollover_resets(self) -> None:
        q = cooldowns.QuotaLedger(limit=2)
        assert q.take(1, day_ordinal=10)
        assert q.take(1, day_ordinal=10)
        assert not q.take(1, day_ordinal=10)
        assert q.take(1, day_ordinal=11)


class TestParseCommand:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/isalive", "isalive"),
            ("/tavivo", "isalive"),  # v1 pt-BR alias
            ("/configurar", "config"),  # v1 name -> QA name
            ("/deleteposts", "deletereposts"),
            ("/shippar @x", "ship"),
            ("/trex", "con_trex"),  # spec'd in QA, missing from v1
        ],
    )
    def test_aliases_map_to_canonical(self, text: str, expected: str) -> None:
        parsed = textmatch.parse_command(text, "CookieMWbot")
        assert parsed is not None
        assert parsed.name == expected

    def test_addressed_at_this_bot(self) -> None:
        parsed = textmatch.parse_command("/isalive@CookieMWbot", "CookieMWbot")
        assert parsed is not None and parsed.name == "isalive"

    def test_addressed_at_another_bot_is_ignored(self) -> None:
        assert textmatch.parse_command("/isalive@OtherBot", "CookieMWbot") is None

    def test_dice_shorthand_carries_sides(self) -> None:
        parsed = textmatch.parse_command("/d20", "CookieMWbot")
        assert parsed is not None
        assert parsed.name == "dice" and parsed.args == "20"

    @pytest.mark.parametrize("text", ["", "hello", "not/a/command", " /isalive"])
    def test_non_commands(self, text: str) -> None:
        assert textmatch.parse_command(text, "CookieMWbot") is None

    def test_args_are_preserved(self) -> None:
        parsed = textmatch.parse_command("/youtube how to make a cake", "CookieMWbot")
        assert parsed is not None and parsed.args == "how to make a cake"


class TestLinks:
    def test_finds_supported_hosts(self) -> None:
        text = "look https://bsky.app/profile/a/post/1 and https://x.com/u/status/2"
        assert len(textmatch.find_embeddable_links(text)) == 2

    def test_ignores_unsupported(self) -> None:
        assert textmatch.find_embeddable_links("https://example.com/x") == []

    def test_fast_path_on_text_without_urls(self) -> None:
        assert textmatch.find_embeddable_links("just chatting") == []


class TestRecentIds:
    def test_detects_repeat(self) -> None:
        r = dedupe.RecentIds(capacity=4)
        assert not r.seen(1)
        assert r.seen(1)

    def test_evicts_oldest_not_everything(self) -> None:
        """v1 cleared the whole set at the cap, reopening the duplicate window."""
        r = dedupe.RecentIds(capacity=3)
        for i in (1, 2, 3, 4):
            r.seen(i)
        assert 1 not in r  # oldest evicted
        assert r.seen(4)  # newest still remembered
        assert len(r) == 3

    def test_fingerprint_is_stable_and_short(self) -> None:
        a = dedupe.fingerprint(b"abc")
        assert a == dedupe.fingerprint(b"abc")
        assert a != dedupe.fingerprint(b"abd")
        assert len(a) == 32


class TestCaptcha:
    def test_arithmetic_answer_is_in_options(self) -> None:
        ch = captcha.make_arithmetic()
        assert ch.answer in ch.options
        assert len(ch.options) == 4
        assert len(set(ch.options)) == 4

    def test_emoji_answer_is_in_options(self) -> None:
        ch = captcha.make_emoji(4)
        assert ch.answer in ch.options and len(ch.options) == 4

    def test_verify(self) -> None:
        assert captcha.verify("7", "7")
        assert not captcha.verify("7", "8")
        assert not captcha.verify("", "")

    def test_callback_roundtrip(self) -> None:
        payload = captcha.callback_payload("abc123", "7")
        assert len(payload.encode()) <= 64
        assert captcha.parse_callback(payload) == ("abc123", "7")

    def test_callback_rejects_foreign_payload(self) -> None:
        assert captcha.parse_callback("GIVEAWAY yes") == ("", "")


class TestMetricsServer:
    """`/metrics` must actually serve, not just start.

    `start_http_server(port, registry=None)` starts happily and then 500s on
    every scrape (`AttributeError: 'NoneType' object has no attribute 'collect'`),
    so a single-process deployment — anything without PROMETHEUS_MULTIPROC_DIR —
    exported nothing at all and looked healthy while doing it.
    """

    def test_scrape_returns_metrics_without_multiproc_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import urllib.request

        from cb_core import metrics

        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
        port = 9408
        metrics.start_metrics_server(port, "cb-test", "0.0.0", cython_compiled=False)

        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read()

        assert b"cb_build_info" in body, body[:400]
