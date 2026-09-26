"""Deciding which spoken words get silenced.

The hard part of this project is not the audio, it is this file. Two failures
matter and they pull in opposite directions:

* A word gets through, a five-year-old repeats it, and the whole service was
  pointless.
* A word is silenced that never happened - "classic" clipped because it
  contains a rude substring, or a sermon's "Jesus Christ" cut out of a
  Christmas episode - and the track becomes unwatchable noise.

So everything here is whole-word by construction, compound forms are listed
rather than guessed at, and the one genuinely ambiguous category (the Lord's
name) carries context rules and is flagged for review instead of being trusted.

Matching runs over the ASR's word list, not over a flat string, because every
match has to come back with the timestamps that will be muted.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import NamedTuple

# ---------------------------------------------------------------------------
#  Categories
# ---------------------------------------------------------------------------
#  Each is switched on or off independently in settings. The names are the
#  keys used in config and in the API.

CATEGORIES = ("strong", "mild", "blasphemy", "slurs_sexual")

CATEGORY_LABELS = {
    "strong": "Strong profanity",
    "mild": "Milder swearing",
    "blasphemy": "God's name misused",
    "slurs_sexual": "Slurs and crude sexual terms",
}

# ---------------------------------------------------------------------------
#  The lists
# ---------------------------------------------------------------------------
#  Single words. Suffixes (-s, -ed, -ing, -in', -er, -y) are handled by the
#  matcher, so list the stem only. Compounds are listed in full because
#  guessing at them is how "assessment" ends up silenced.

STRONG = [
    "fuck", "fucker", "fuckers", "fuckin", "fucking", "fucked", "fucks",
    "motherfuck", "motherfucker", "motherfuckers", "motherfucking",
    "clusterfuck", "fuckup", "fuckwit", "fuckhead", "fuckboy",
    "shit", "shits", "shitty", "shitting", "shitted", "shithead", "shithole",
    "shitshow", "shitstorm", "bullshit", "horseshit", "dipshit", "batshit",
    "shitface", "shitbag", "shitload", "apeshit",
    "cunt", "cunts",
    "twat", "twats",
    "wanker", "wankers", "wank",
    "bollocks", "bollock",
    "prick", "pricks",
    "arsehole", "asshole", "assholes",
]

MILD = [
    # "damning" is deliberately absent: "damning evidence" is ordinary English
    # and was muted once on exactly that line.
    "damn", "damned", "damnit", "dammit", "goddamnit",
    "hell", "hells",
    "ass", "asses", "arse", "arses", "jackass", "dumbass", "badass", "smartass",
    "bitch", "bitches", "bitching", "bitchy",
    "bastard", "bastards",
    "crap", "craps", "crappy", "crapping",
    "piss", "pissed", "pissing", "pisser",
    "douche", "douchebag",
    "bugger", "buggered",
    # Deliberately absent: "bloody" and "screwed". Both are ordinary words far
    # more often than not ("a bloody nose", "screwed in the bulb"), and a
    # cleaned track that clips those reads as broken. Add them under custom
    # words in settings if a particular show needs it.
]

# The Lord's name. Exclamations only - see REVERENT_CONTEXT below.
BLASPHEMY = [
    "goddamn", "goddamned", "goddamnit",
    "godsake",
]

class Phrase(NamedTuple):
    """A phrase to recognise, and which of its words actually get silenced.

    The two are deliberately separate. The PHRASE is what decides this is an
    exclamation rather than reverent speech - "god" on its own is ambiguous,
    "oh my god" is not - but only some of its words are the ones nobody wants
    to hear.

    Silencing the whole phrase takes "oh my" and "for the love of" with it,
    which is a second of dialogue lost to censor one word, and it is what makes
    a cleaned track sound chopped about. So `mute` names the positions that are
    actually profane and everything else stays audible.
    """

    words: tuple[str, ...]
    mute: tuple[int, ...]


# Positions count from zero, as their place in `words`.
BLASPHEMY_PHRASES = [
    # Both halves are the swear here, and they are adjacent, so this comes out
    # as one span regardless.
    Phrase(("god", "damn"), mute=(0, 1)),
    Phrase(("god", "damn", "it"), mute=(0, 1)),          # "it" survives
    Phrase(("jesus", "christ"), mute=(0, 1)),
    Phrase(("jesus", "h", "christ"), mute=(0, 2)),       # the "H" is not a swear
    Phrase(("oh", "my", "god"), mute=(2,)),              # "oh my" survives
    Phrase(("my", "god"), mute=(1,)),
    Phrase(("oh", "god"), mute=(1,)),
    Phrase(("for", "god's", "sake"), mute=(1,)),
    Phrase(("for", "christ's", "sake"), mute=(1,)),
    Phrase(("for", "the", "love", "of", "god"), mute=(4,)),
    Phrase(("swear", "to", "god"), mute=(2,)),
    Phrase(("god", "almighty"), mute=(0,)),
    Phrase(("christ", "almighty"), mute=(0,)),
    Phrase(("holy", "christ"), mute=(1,)),               # "holy" survives
    Phrase(("sweet", "jesus"), mute=(1,)),
    Phrase(("jesus", "wept"), mute=(0,)),
]

# A bare "jesus" or "christ" said alone is nearly always an exclamation; said
# inside any of these it is not. The window is the four words either side.
REVERENT_CONTEXT = {
    "lord", "savior", "saviour", "christ's", "pray", "prayer", "praying",
    "prayed", "amen", "bible", "scripture", "gospel", "church", "faith",
    "believe", "believes", "believed", "risen", "resurrection", "cross",
    "crucified", "born", "birth", "disciples", "apostle", "blessed", "bless",
    "blessing", "holy", "worship", "hallelujah", "praise", "praises",
    "thank", "thanks", "thankful", "grace", "mercy", "heaven", "salvation",
    "son", "father", "gods", "name", "sermon", "preach", "preaching",
    "testament", "verse", "psalm", "baptized", "baptism", "communion",
}

# Words that are part of the category but read as reverent on their own.
BLASPHEMY_SOLO = ["jesus", "christ", "christsake"]

SLURS_SEXUAL = [
    # Racial and ethnic slurs. Listed so they can be silenced; nothing here is
    # ever shown in the UI except as a timestamp and a category.
    "nigger", "niggers", "nigga", "niggas", "spic", "spics",
    "kike", "kikes", "wetback", "wetbacks", "gook", "gooks",
    # Deliberately absent, on the evidence of this library: "chink" (both
    # occurrences were "a chink in his armor"), "coon"/"coons" (a surname,
    # and raccoons) and "dyke" (a Van Dyke beard). The slur senses are real
    # but have not once turned up here, while the innocent senses have.
    "towelhead", "raghead", "beaner", "beaners",
    # "cracker" is deliberately absent: a slur in one usage and a biscuit in
    # nearly every other, and the audio gives no way to tell them apart.
    # Slurs against sexuality and disability.
    "faggot", "faggots", "fag", "fags", "tranny", "trannies",
    "retard", "retards", "retarded",
    # Crude anatomical and sexual slang.
    "dick", "dicks", "dickhead", "dickwad", "cock", "cocks", "cocksucker",
    "pussy", "pussies", "tits", "titties", "titty", "boobs", "boob",
    "whore", "whores", "slut", "sluts", "slutty", "skank", "hooker", "hookers",
    "blowjob", "blowjobs", "handjob", "rimjob", "jizz", "cum", "cumming",
    "wank", "jerkoff", "boner", "horny", "nutsack", "ballsack", "bollocking",
]

WORDLISTS = {
    "strong": STRONG,
    "mild": MILD,
    "blasphemy": BLASPHEMY,
    "slurs_sexual": SLURS_SEXUAL,
}

# ---------------------------------------------------------------------------
#  Words that must never be silenced
# ---------------------------------------------------------------------------
#  Whole-word matching stops "class" and "assassin" on its own. This list is
#  for real words that ARE on a list above in some other sense, or that the
#  ASR is known to hand back in place of something innocent. Editable in
#  settings, because only the household knows that Grandma is called Fanny.

NEVER = {
    "hello", "shell", "shelter", "bass", "class", "classic", "pass", "passed",
    "grass", "glass", "mass", "massive", "assist", "assign", "assume",
    "assess", "asset", "assets", "association", "assassin", "embarrass",
    "cassette", "compass", "cocktail", "cockpit", "peacock", "shuttlecock",
    "cumulative", "cucumber", "circumstance", "document", "accumulate",
    "titles", "title", "titan", "titanic", "constitution", "competition",
    "scrap", "scrapbook", "dickens", "dickinson", "cricket", "thicket",
    "shiitake", "analysis", "canal", "dam", "damascus", "damage", "damages",
    "hellenic", "hellman", "shitake", "buggy", "bugle",
}

# ---------------------------------------------------------------------------
#  Matching
# ---------------------------------------------------------------------------

# Only plurals and possessives are grown automatically. Verb and adjective
# endings are not, because short stems grow into ordinary words: cock + y is
# "cocky", cock + er is "cocker" (spaniel), boob + y is "booby" (trap), damn +
# ing is "damning evidence". All four were muted in this library on exactly
# those innocent lines. Every form that IS wanted - fucking, shitty, bitching,
# pissed - is written out in the lists above, where it can be seen.
_SUFFIXES = ("", "s", "es", "'s")

_STRIP = re.compile(r"^[^\w']+|[^\w']+$")
_APOS = dict.fromkeys(map(ord, "‘’ʼ"), "'")


def normalize(token: str) -> str:
    """ASR output to a bare comparable word: 'Fucking,' -> 'fucking'."""
    text = unicodedata.normalize("NFKC", str(token or "")).translate(_APOS).lower()
    return _STRIP.sub("", text)


def _stem_forms(word: str) -> set[str]:
    """Every spelling of a listed stem the matcher should accept."""
    forms = {word}
    for suffix in _SUFFIXES:
        forms.add(word + suffix)
    return forms


@dataclass(frozen=True)
class Match:
    """One thing to silence."""
    start: float
    end: float
    text: str
    category: str
    needs_review: bool = False
    reason: str = ""


@dataclass
class Matcher:
    categories: tuple[str, ...] = ("strong", "mild", "blasphemy", "slurs_sexual")
    never: frozenset[str] = frozenset(NEVER)
    extra: tuple[str, ...] = ()           # household additions, always censored
    # Words whose surroundings are allowed to save them - the same list the
    # second opinion uses. A word that is not on it is muted wherever it
    # appears, including "Jesus" in a prayer, which is the point of saying
    # "always mute this one" and meaning it.
    context_words: frozenset[str] = frozenset({"jesus", "christ"})
    _index: dict[str, str] = field(default_factory=dict, init=False)
    _never: frozenset[str] = field(default_factory=frozenset, init=False)

    def __post_init__(self) -> None:
        index: dict[str, str] = {}
        for category in self.categories:
            for word in WORDLISTS.get(category, ()):
                for form in _stem_forms(word):
                    index.setdefault(form, category)
            if category == "blasphemy":
                for word in BLASPHEMY_SOLO:
                    index.setdefault(word, "blasphemy")
        for word in self.extra:
            word = normalize(word)
            if word:
                index[word] = "custom"
        self._never = frozenset(normalize(w) for w in self.never)
        for word in self._never:
            index.pop(word, None)
        self._index = index

    # -- helpers ---------------------------------------------------------
    def _reverent_nearby(self, words: list[dict], i: int, window: int = 4) -> bool:
        lo, hi = max(0, i - window), min(len(words), i + window + 1)
        for j in range(lo, hi):
            if j == i:
                continue
            if normalize(words[j].get("word", "")) in REVERENT_CONTEXT:
                return True
        return False

    def _phrase_at(self, norms: list[str], i: int) -> Phrase | None:
        """The longest blasphemy phrase starting at i, if any.

        Longest wins so that "god damn it" is not settled by "god damn" before
        the third word is looked at.
        """
        best: Phrase | None = None
        for phrase in BLASPHEMY_PHRASES:
            end = i + len(phrase.words)
            if end <= len(norms) and tuple(norms[i:end]) == phrase.words:
                if best is None or len(phrase.words) > len(best.words):
                    best = phrase
        return best

    # -- the entry point -------------------------------------------------
    def find(self, words: list[dict]) -> list[Match]:
        """`words` is the ASR's word list: {word, start, end}.

        Returns matches in time order. Overlapping or touching matches are
        left as they are; the caller merges them into mute spans.
        """
        norms = [normalize(w.get("word", "")) for w in words]
        out: list[Match] = []
        i = 0
        blasphemy_on = "blasphemy" in self.categories

        while i < len(norms):
            token = norms[i]
            if not token:
                i += 1
                continue

            # Phrases first, so "god damn" is one match rather than two, and so
            # a phrase wins over the solo-word rules below.
            if blasphemy_on:
                phrase = self._phrase_at(norms, i)
                if phrase:
                    end_i = i + len(phrase.words)
                    forgiving = any(w in self.context_words for w in phrase.words)
                    if not (forgiving and self._reverent_nearby(words, i, window=3)):
                        # One match per profane WORD, not one across the whole
                        # phrase: "oh my god" silences "god" and leaves "oh my"
                        # audible. Where the muted words are adjacent - "god
                        # damn" - to_spans merges them back into one span, so
                        # nothing is chopped that should not be.
                        spoken = " ".join(phrase.words)
                        for offset in phrase.mute:
                            # The never list wins here too: a name Whisper
                            # keeps hearing as "god" is fixed by listing it.
                            if norms[i + offset] in self._never:
                                continue
                            word = words[i + offset]
                            out.append(Match(
                                start=float(word.get("start", 0.0)),
                                end=float(word.get("end", 0.0)),
                                text=str(word.get("word", "")).strip(),
                                category="blasphemy",
                                needs_review=True,
                                reason="the Lord's name used as an exclamation"
                                       f" (in “{spoken}”)",
                            ))
                    i = end_i
                    continue

            category = self._index.get(token)
            if category:
                solo_blasphemy = category == "blasphemy" and token in BLASPHEMY_SOLO
                if (solo_blasphemy and token in self.context_words
                        and self._reverent_nearby(words, i)):
                    i += 1
                    continue
                out.append(Match(
                    start=float(words[i].get("start", 0.0)),
                    end=float(words[i].get("end", 0.0)),
                    text=str(words[i].get("word", "")).strip(),
                    category=category,
                    needs_review=solo_blasphemy,
                    reason="said on its own, so read as an exclamation" if solo_blasphemy else "",
                ))
            i += 1

        return out


def to_spans(matches: list[Match], pad_start: float, pad_end: float,
             gap: float = 0.12) -> list[tuple[float, float]]:
    """Matches to the spans that actually get silenced.

    Padding covers the ASR clipping the head or tail of a word - better a
    silenced breath than an audible first consonant. Two spans closer together
    than `gap` become one, because a 50ms blip of audio between two muted words
    sounds like a fault rather than a pause.
    """
    spans: list[tuple[float, float]] = []
    for m in sorted(matches, key=lambda m: m.start):
        start = max(0.0, m.start - pad_start)
        end = max(start, m.end + pad_end)
        start, end = round(start, 3), round(end, 3)
        if spans and start - spans[-1][1] <= gap:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    return spans
