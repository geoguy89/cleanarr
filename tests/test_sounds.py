"""Telling a word Whisper misheard from one a captioner softened, by sound."""

from __future__ import annotations

import pytest

from censarr import sounds


@pytest.mark.parametrize("heard, shown", [
    ("cock", "caulk"), ("slut", "slob"), ("fuck", "fork"), ("shit", "sheet"),
    ("bitch", "beach"), ("hell", "hail"), ("dick", "dig"), ("pussies", "purses"),
    ("road to hell", "roosevelt"), ("shit", "ship"), ("ass", "as"), ("god", "good"),
    ("shit", "city"),
])
def test_mishearings_sound_alike(heard, shown):
    assert sounds.sounds_alike(heard, shown)


@pytest.mark.parametrize("heard, shown", [
    ("shit", "whoa"), ("shit", "crap"), ("oh shit", "oh no"), ("shit", "that"),
    ("fuck", "screw"), ("bitch", "jerk"), ("damn it", "come on"),
    ("fucks", "pete's"), ("bitch", "gun"), ("shit", "cow"), ("oh shit", "oh man"),
    ("bitch", "brat"), ("pissed", "pleased"),
])
def test_softenings_do_not(heard, shown):
    assert not sounds.sounds_alike(heard, shown)


def test_minced_oaths_are_softenings_however_close_they_sound():
    assert sounds.similarity("fucking", "freaking") > sounds.SOUNDALIKE
    assert all(sounds.softened(w) for w in ("freaking", "heck", "darn", "gosh", "Frickin'",
                                            "freak", "flaming", "peeved"))
    assert not sounds.softened("caulk")


def test_unknown_words_never_sound_alike():
    assert sounds.phones("Cabreras") is None
    assert not sounds.sounds_alike("hell", "Cabreras")


def test_a_dropped_g_sounds_the_same():
    assert sounds.sounds_alike("fucking", "fuckin'")


@pytest.mark.parametrize("dub", ["sucker", "flipper", "flipped", "fudged", "freaked",
                                 "trucker", "monkey", "dipstick"])
def test_tv_dubs_that_sound_close_are_softenings(dub):
    """Each came back as a sound-alike of the swear it stands in for - "sucker"
    for "fucker" scored 0.88 - and the swear was left in."""
    assert sounds.softened(dub)


@pytest.mark.parametrize("oath, swear", [
    ("doggone", "goddamn"), ("feck", "fuck"), ("bish", "bitch"), ("peed", "piss"),
    ("balls", "bollocks"), ("bother", "bugger"), ("blast", "bastard"), ("heaven", "hell"),
])
def test_old_fashioned_and_regional_stand_ins_are_softenings(oath, swear):
    """"Oh, bother" for "Oh, bugger", "What in heaven" for "What the hell":
    each came back as a sound-alike (0.66-0.9) and left the swear in."""
    assert sounds.softened(oath)
