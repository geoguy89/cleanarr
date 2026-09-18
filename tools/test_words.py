"""What gets silenced, and more importantly what does not.

Run: python tools/test_words.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from cleanarr.words import Matcher, to_spans  # noqa: E402

failures: list[str] = []


def words(sentence: str, start: float = 0.0, step: float = 0.4) -> list[dict]:
    """A sentence as the ASR would hand it over: one word, one timestamp."""
    out = []
    t = start
    for w in sentence.split():
        out.append({"word": w, "start": round(t, 2), "end": round(t + step * 0.8, 2)})
        t += step
    return out


def censored(sentence: str, **kw) -> list[str]:
    m = Matcher(**kw)
    return [hit.text.strip().lower().strip(",.!?") for hit in m.find(words(sentence))]


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n       got:  {got!r}\n       want: {want!r}")
        failures.append(name)


print("the words it is for")
check("plain", censored("what the fuck is that"), ["fuck"])
check("inflected", censored("he is fucking useless"), ["fucking"])
check("compound", censored("that is bullshit and you know it"), ["bullshit"])
check("punctuation attached", censored("Shit! That hurt."), ["shit"])
check("two in one line", censored("shit that is a damn lie"), ["shit", "damn"])
check("mild category", censored("this is a damn mess"), ["damn"])
check("slur category", censored("he called him a faggot"), ["faggot"])

print("the words it must leave alone")
for phrase in ("she is in my class", "an assassin in the grass",
               "pass me the glasses", "the cockpit door", "a cocktail party",
               "shall we assume the assignment", "hello there",
               "the title of the document", "scrap that idea",
               "bass guitar", "cucumber sandwiches", "massive damage",
               "a bloody nose", "he screwed in the bulb"):
    check(f"leaves alone: {phrase}", censored(phrase), [])

print("categories can be switched off")
check("mild off, strong still on",
      censored("this damn shit", categories=("strong",)), ["shit"])
check("strong off, mild still on",
      censored("this damn shit", categories=("mild",)), ["damn"])
check("all off", censored("this damn shit", categories=()), [])

print("the Lord's name, which is the hard one")
# The PHRASE is what proves it is an exclamation; only the profane WORD inside
# it is silenced. Muting "oh my" as well is a second of dialogue lost to censor
# one word, and it is what makes a cleaned track sound hacked about.
check("exclamation", censored("oh my god that is huge"), ["god"])
check("leaves the rest of the phrase alone",
      censored("for the love of god stop"), ["god"])
check("holy christ keeps the holy", censored("holy christ look at that"), ["christ"])
check("swear to god", censored("i swear to god i did"), ["god"])
check("for god's sake", censored("for god's sake listen"), ["god's"])
check("god damn is profane in both halves",
      censored("god damn it all"), ["god", "damn"])
check("goddamn as one word", censored("the goddamn car"), ["goddamn"])
check("bare christ", censored("christ that was close"), ["christ"])
check("reverent: prayer", censored("we pray in the name of jesus christ amen"), [])
check("reverent: worship", censored("praise god for his mercy"), [])
check("reverent: teaching",
      censored("the gospel says jesus christ is lord"), [])
check("reverent: thanks", censored("thank god you are safe"), [])
check("blasphemy off leaves it entirely",
      censored("oh my god", categories=("strong", "mild")), [])

print("household exceptions")
check("a never-word can be added",
      censored("pass the crab dip", never=frozenset({"crab"})), [])
check("a custom word can be added",
      censored("what a muppet", extra=("muppet",)), ["muppet"])

print("review flags")
m = Matcher()
flags = {h.text.lower(): h.needs_review for h in m.find(words("oh my god that fucking hurts"))}
# The match is the single word now, not the phrase around it - but it still
# carries the review flag, and its reason still names the phrase it came from,
# so the Cleaned page can explain why "god" on its own was silenced.
check("the Lord's name is flagged for review", flags.get("god"), True)
check("plain profanity is not", flags.get("fucking"), False)
reasons = {h.text.lower(): h.reason for h in m.find(words("oh my god that hurts"))}
check("the reason names the phrase it came from",
      "oh my god" in reasons.get("god", ""), True)

print("phrase spans stay tight")
_m = Matcher()
_hits = _m.find(words("oh my god that is huge"))
_spans = to_spans(_hits, 0.12, 0.12)
check("only the profane word is inside the span", len(_spans), 1)
# "oh" starts at 0.0 and "my" at 0.4; "god" starts at 0.8. With 0.12 of pad the
# span must not reach back before "my" has finished being said.
check("the span starts at the swear, not the phrase", _spans[0][0] > 0.6, True)

_adjacent = to_spans(Matcher().find(words("god damn it all")), 0.12, 0.12)
check("adjacent swears merge into one span", len(_adjacent), 1)

print("spans")
hits = Matcher().find(words("shit shit", step=0.4))
check("two near words become one span",
      to_spans(hits, 0.12, 0.12), [(0.0, 0.84)])
far = [{"word": "shit", "start": 1.0, "end": 1.3},
       {"word": "shit", "start": 10.0, "end": 10.3}]
check("two distant words stay separate",
      to_spans(Matcher().find(far), 0.1, 0.1), [(0.9, 1.4), (9.9, 10.4)])
check("padding never goes below zero",
      to_spans([m for m in Matcher().find([{"word": "shit", "start": 0.05, "end": 0.3}])],
               0.2, 0.2), [(0.0, 0.5)])

print()
if failures:
    print(f"{len(failures)} failed: {', '.join(failures)}")
    sys.exit(1)
print("all word checks passed")
