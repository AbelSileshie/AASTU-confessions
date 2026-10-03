import pytest

from textfilter import (
    blocked_words,
    check_text,
    has_link,
    has_mention,
    has_phone,
    highlight,
    normalize,
    risk_words,
    text_hash,
    valid_ad_url,
)


class TestNormalize:
    def test_lowercases_and_collapses_repeats(self):
        assert normalize("Soooo GOOD") == "so god"

    def test_maps_common_swaps(self):
        assert normalize("phone qale") == "fone kale"

    def test_strips_invisible_and_punctuation(self):
        assert normalize("he\u200bllo, world!!") == "helo world"

    def test_keeps_amharic(self):
        assert normalize("ሰላም World") == "ሰላም world"


class TestHash:
    def test_same_meaning_same_hash(self):
        assert text_hash("Hello   WORLD!") == text_hash("hello world")

    def test_different_text_different_hash(self):
        assert text_hash("a secret") != text_hash("another secret")


class TestDetectors:
    @pytest.mark.parametrize("s", ["see https://x.com", "visit www.foo.et now", "t.me/abc", "go to example.com"])
    def test_links_detected(self, s):
        assert has_link(s)

    def test_plain_text_not_link(self):
        assert not has_link("I love pizza. Really.")

    def test_mentions(self):
        assert has_mention("ask @someone_123")
        assert not has_mention("email me at a@b")  # too short handle

    @pytest.mark.parametrize("s", ["call 0911223344", "+251 911 22 33 44", "09 11 22 33 44"])
    def test_phones(self, s):
        assert has_phone(s)

    def test_year_is_not_phone(self):
        assert not has_phone("back in 2019 I was 20")

    def test_zero_width_evasion_fails(self):
        assert has_link("ht\u200btps://evil.com")
        assert has_phone("09\u200b11223344")


class TestWordlists:
    def test_block_matches_variants(self):
        assert blocked_words("that was sooo baaad", ["bad"]) == ["bad"]

    def test_block_requires_whole_word(self):
        assert blocked_words("assistant", ["ass"]) == []

    def test_phrase_match(self):
        assert risk_words("send nude pics", ["nude pic"]) == ["nude pic"]

    def test_highlight_wraps(self):
        assert highlight("a video of me", ["video"]) == "a ⚠️video⚠️ of me"


class TestCheckText:
    kw = dict(min_len=5, max_len=50, blocklist=["spam"], risklist=["video"])

    def test_ok_with_risk(self):
        r = check_text("there is a video of me", **self.kw)
        assert r.ok and r.risk_hits == ["video"]

    def test_order_short_first(self):
        assert check_text("hi", **self.kw).reason == "too_short"

    def test_too_long(self):
        assert check_text("x" * 51, **self.kw).reason == "too_long"

    def test_link(self):
        assert check_text("visit https://a.com ok", **self.kw).reason == "link"

    def test_mention(self):
        assert check_text("hey @someone_here", **self.kw).reason == "mention"

    def test_phone(self):
        assert check_text("call 0911223344 pls", **self.kw).reason == "phone"

    def test_blocked(self):
        assert check_text("this is spam ok", **self.kw).reason == "blocked"


class TestAdUrl:
    def test_https_ok(self):
        assert valid_ad_url("https://shop.example.com/x?y=1")

    def test_http_rejected(self):
        assert not valid_ad_url("http://shop.example.com")

    def test_javascript_rejected(self):
        assert not valid_ad_url("javascript:alert(1)")

    def test_invite_links_rejected(self):
        assert not valid_ad_url("https://t.me/joinchat/abc")
        assert not valid_ad_url("https://t.me/+abc")

    def test_shortener_toggle(self):
        assert not valid_ad_url("https://bit.ly/x")
        assert valid_ad_url("https://bit.ly/x", allow_shorteners=True)

    def test_too_long(self):
        assert not valid_ad_url("https://a.com/" + "x" * 600)
