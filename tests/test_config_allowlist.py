"""The allowlist of user ids parses ASCII digits, and nothing else.

`str.isdigit()` is true for the Unicode digit characters, and `int()`
accepts them: `int("٣")` is 3. The old one-liner
(`{int(x) for x in raw.split(",") if x.strip().isdigit()}`) therefore
turned an id written in Arabic-Indic digits into user 3 -- an
allowlist that authorises a different person than the one written
down. It is a security boundary, so it parses exactly what it looks
like; anything it does not understand is not an id.

From the external review of 2026-10-28 (the nit about `.isdigit()`).
"""
import config


def test_plain_ascii_ids_are_parsed():
    assert config._parse_user_ids("111,222") == {111, 222}


def test_whitespace_around_the_ids_is_tolerated():
    assert config._parse_user_ids(" 111 , 222 ") == {111, 222}


def test_a_unicode_digit_is_not_an_id():
    # The character is a digit by Unicode's own definition, and int()
    # would happily return 3 for it. That is exactly the hole.
    assert "٣".isdigit()
    assert int("٣") == 3
    assert config._parse_user_ids("٣") == set()


def test_a_unicode_digit_does_not_authorise_its_ascii_value():
    assert config._parse_user_ids("٣") != {3}


def test_junk_is_dropped_rather_than_guessed():
    assert config._parse_user_ids("111,abc,,222, 3.5") == {111, 222}
    assert config._parse_user_ids("") == set()
