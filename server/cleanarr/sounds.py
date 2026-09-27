"""Whether two bits of speech sound alike.

When the subtitles have a different word where Whisper heard a swear, one of
two things happened. Whisper misheard a harmless word as a swear - "caulk" and
"cock" are pronounced identically - or the captioner softened a swear it did
hear ("Oh, shit!" captioned "Whoa!"). The first should be left in, the second
muted. What separates them is sound: a mishearing sounds like the swear, a
softening usually does not.

Pronunciations come from the CMU Pronouncing Dictionary (data/, BSD licence).
Two strings are compared phoneme by phoneme, with near sounds (two vowels, two
stops) cheaper to swap than unrelated ones. Measured:

    sound-alikes   caulk 1.0, sheet 0.9, beach 0.9, hail 0.9, dig 0.83,
                   slob 0.8, fork 0.72, duck 0.67
    softenings     "come on" for "damn it" 0.58, "oh no" 0.47,
                   crap 0.35, whoa 0.3, screw 0.27

Minced oaths - freaking, heck, darn, gosh - sound close on purpose, so they are
listed and always count as softening. A word the dictionary does not know
counts as not sounding alike: the swear stays muted.
"""

from __future__ import annotations

import gzip
import re
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "cmudict.dict.gz"

# At or above this, the subtitle's word sounds like what Whisper heard.
SOUNDALIKE = 0.65

# A captioner's stand-ins for swears. They sound like the swear on purpose.
MINCED = frozenset("""
    freaking freakin frickin fricking frick fricken frig frigging friggin flipping flippin
    effing effin eff frak frakking fracking bleep bleeping bleepin
    darn darned darnit dang danged dagnabbit dadgum dern goldarn
    heck gosh golly gee geez jeez jeepers jeeze sheesh
    crap crappy crud shoot shucks fudge fudging sugar
    butt butthole jerk screw screwed screwing
    mothertrucker motherfudger
""".split())

_CLASS = {}
for _cls, _phones in {
        "vowel": "AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW",
        "stop": "P B T D K G",
        "fricative": "F V TH DH S Z SH ZH HH",
        "affricate": "CH JH",
        "nasal": "M N NG",
        "liquid": "L R",
        "glide": "W Y"}.items():
    for _p in _phones.split():
        _CLASS[_p] = _cls

_WORD = re.compile(r"[a-z']+")
_GAP = 0.8          # adding or dropping a sound


@lru_cache(maxsize=1)
def _dictionary() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    with gzip.open(DATA, "rt", encoding="utf8") as handle:
        for line in handle:
            parts = line.split("#", 1)[0].split()
            if len(parts) < 2:
                continue
            word = re.sub(r"\(\d+\)$", "", parts[0])
            # The first pronunciation listed is the common one.
            out.setdefault(word, [re.sub(r"\d", "", p) for p in parts[1:]])
    return out


def phones(text: str) -> list[str] | None:
    """The sounds of `text`, or None when any word in it is unknown."""
    known = _dictionary()
    out: list[str] = []
    for word in _WORD.findall(str(text or "").lower()):
        word = word.strip("'")
        if not word:
            continue
        found = known.get(word) or known.get(word + "g")      # fuckin' -> fucking
        if found is None:
            return None
        out += found
    return out or None


def _swap(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if _CLASS.get(a) == _CLASS.get(b):
        return 0.3 if _CLASS.get(a) == "vowel" else 0.5
    return 1.0


def similarity(a: str, b: str) -> float | None:
    """0-1: how alike `a` and `b` sound. None when either cannot be pronounced."""
    x, y = phones(a), phones(b)
    if not x or not y:
        return None
    prev = [j * _GAP for j in range(len(y) + 1)]
    for i in range(1, len(x) + 1):
        row = [i * _GAP]
        for j in range(1, len(y) + 1):
            row.append(min(prev[j] + _GAP, row[j - 1] + _GAP,
                           prev[j - 1] + _swap(x[i - 1], y[j - 1])))
        prev = row
    return round(1 - prev[-1] / max(len(x), len(y)), 2)


def softened(word: str) -> bool:
    """A captioner's stand-in for a swear: freaking, heck, darn."""
    return str(word or "").lower().strip("'") in MINCED


# Unless they are this close, a sound-alike must also start with the same
# sound: short words differ by a sound or two whatever they are, and "that"
# scored 0.73 against "shit" - a caption's "Let me get that" for "Oh, shit!".
# Every real mishearing measured shares its first sound: caulk, slob, sheet.
ONSET_EXEMPT = 0.85


def sounds_alike(heard: str, shown: str) -> bool:
    """Whether the subtitle's `shown` sounds like what Whisper `heard`."""
    x, y = phones(heard), phones(shown)
    score = similarity(heard, shown)
    if score is None or not x or not y:
        return False
    if x[0] != y[0] and score < ONSET_EXEMPT:
        return False
    return score >= SOUNDALIKE
