"""Telling a word Whisper misheard from one a captioner softened, by sound."""

from __future__ import annotations

import pytest

from cleanarr import sounds


@pytest.mark.parametrize("heard, shown", [
    ("cock", "caulk"), ("slut", "slob"), ("fuck", "fork"), ("shit", "sheet"),
    ("bitch", "beach"), ("hell", "hail"), ("dick", "dig"), ("pussies", "purses"),
])
def test_mishearings_sound_alike(heard, shown):
    assert sounds.sounds_alike(heard, shown)


@pytest.mark.parametrize("heard, shown", [
    ("shit", "whoa"), ("shit", "crap"), ("oh shit", "oh no"), ("shit", "that"),
    ("fuck", "screw"), ("bitch", "jerk"), ("damn it", "come on"),
])
def test_softenings_do_not(heard, shown):
    assert not sounds.sounds_alike(heard, shown)


def test_minced_oaths_are_softenings_however_close_they_sound():
    assert sounds.similarity("fucking", "freaking") > sounds.SOUNDALIKE
    assert all(sounds.softened(w) for w in ("freaking", "heck", "darn", "gosh", "Frickin'"))
    assert not sounds.softened("caulk")


def test_unknown_words_never_sound_alike():
    assert sounds.phones("Cabreras") is None
    assert not sounds.sounds_alike("hell", "Cabreras")


def test_a_dropped_g_sounds_the_same():
    assert sounds.sounds_alike("fucking", "fuckin'")
