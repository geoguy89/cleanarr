"""Whether two bits of speech sound alike.

When the subtitles have a different word where Whisper heard a swear, one of
two things happened. Whisper misheard a harmless word as a swear - "caulk" and
"cock" are pronounced identically - or the captioner softened a swear it did
hear ("Oh, shit!" captioned "Whoa!"). The first should be left in, the second
muted. What separates them is sound: a mishearing sounds like the swear, a
softening usually does not.

Pronunciations come from the CMU Pronouncing Dictionary (data/, BSD licence).
Two strings are compared sound by sound, the way speech runs together: near
sounds cost less to swap (two vowels; two sounds made in the same place, like
d and z), and the sounds running speech swallows - h, t, d, the "uh" and "oo"
of little words - cost less to lose. That is what lets a phrase match a word:
Whisper wrote "the road to hell" where the line was "the Roosevelt" (0.74).
Measured:

    sound-alikes   caulk 1.0, sheet 0.9, purses 0.88, dig 0.87, slob 0.8,
                   Roosevelt for "road to hell" 0.74, fork 0.72
    softenings     "oh no" 0.57, "get that" 0.53, whoa 0.43, crap 0.35

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
    crap crappy crud shoot shucks fudge fudging sugar shh
    butt butthole jerk screw screwed screwing
    freak freakin flaming flamin peeved ticked teed
    mothertrucker motherfudger
    sucker suckers flipper flippers flipped fudged fudger fudgers freaked frigged
    effed frakked trucker truckers trucking truckin monkey dipstick dipsticks
    doggone feck fecking feckin bish pee peed peeing balls bother blast blasted
    heaven heavens
""".split())

# The small words that hold a sentence together. They sound close to swears -
# "its" and tits 0.9, "am" and damn 0.87, "can't" and cunt 0.82, "here" and
# hell 0.77, "she" and shit 0.77 - but a caption that has one where the swear
# was has reworded the line; it is not what Whisper misheard.
FUNCTION_WORDS = frozenset("""
    a an the this that these those some any each every no
    i me my mine myself you your yours yourself he him his himself she her hers herself
    it its itself we us our ours ourselves they them their theirs themselves
    am is are was were be been being do does did have has had having
    will would shall should can could may might must gonna gotta wanna
    and or but nor so yet if than then as because though although while until unless since
    at by for from in into of off on onto out over to up down with without about after
    before under above around through between against among upon
    not yes yeah oh uh um ah hey well here there where when what who whom whose which why how
    too very just also only
    i'm i've i'll i'd you're you've you'll you'd he's he'll he'd she's she'll she'd it's it'll
    we're we've we'll we'd they're they've they'll they'd that's there's here's what's who's
    where's let's don't doesn't didn't can't won't isn't aren't wasn't weren't hasn't
    haven't hadn't couldn't wouldn't shouldn't mustn't
""".split())
_FUNCTION_PLAIN = frozenset(w.replace("'", "") for w in FUNCTION_WORDS)


def function_word(word: str) -> bool:
    w = str(word or "").lower().replace("’", "'").strip(" ,.!?;:-\"()")
    return w in FUNCTION_WORDS or w.replace("'", "") in _FUNCTION_PLAIN


# Where each consonant is made: a swap within one place is a near miss.
_PLACE = {}
for _place, _phones in {
        "lips": "P B M F V W", "teeth": "TH DH", "ridge": "T D N S Z L R",
        "palate": "SH ZH CH JH Y", "velum": "K G NG", "glottis": "HH"}.items():
    for _p in _phones.split():
        _PLACE[_p] = _place

# Swallowed in running speech: cheap to lose.
_WEAK = frozenset({"HH", "AH", "IH", "UW", "T", "D"})

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


def _gap(sound: str) -> float:
    """Adding or dropping a sound."""
    return 0.4 if sound in _WEAK else 0.8


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
    kind_a, kind_b = _CLASS.get(a), _CLASS.get(b)
    if kind_a == kind_b == "vowel":
        return 0.3
    if kind_a == kind_b:
        return 0.4 if _PLACE.get(a) == _PLACE.get(b) else 0.5
    if _PLACE.get(a) and _PLACE.get(a) == _PLACE.get(b):
        return 0.5
    return 1.0


def similarity(a: str, b: str) -> float | None:
    """0-1: how alike `a` and `b` sound. None when either cannot be pronounced."""
    x, y = phones(a), phones(b)
    if not x or not y:
        return None
    return _score(x, y)


def _score(x: list[str], y: list[str]) -> float:
    prev = [0.0]
    for sound in y:
        prev.append(prev[-1] + _gap(sound))
    for i in range(1, len(x) + 1):
        row = [prev[0] + _gap(x[i - 1])]
        for j in range(1, len(y) + 1):
            row.append(min(prev[j] + _gap(x[i - 1]), row[j - 1] + _gap(y[j - 1]),
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
# The hissing sounds count as one start: a crowd's "City! City!" came out as
# "Shit, shit!" ten times in Ted Lasso S02E08. "That" still does not pass for
# "shit" - th is not a hiss.
_HISS = frozenset({"S", "Z", "SH", "ZH"})


def _same_start(a: str, b: str) -> bool:
    return a == b or (a in _HISS and b in _HISS)


def sounds_alike(heard: str, shown: str) -> bool:
    """Whether the subtitle's `shown` sounds like what Whisper `heard`."""
    return phones_alike(phones(heard), phones(shown))


def phones_alike(x: list[str] | None, y: list[str] | None) -> bool:
    """sounds_alike, for two strings of sounds."""
    if not x or not y:
        return False
    score = _score(x, y)
    if not _same_start(x[0], y[0]) and score < ONSET_EXEMPT:
        return False
    return score >= SOUNDALIKE
