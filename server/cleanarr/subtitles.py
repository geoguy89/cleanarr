"""Checking what Whisper heard against the file's own subtitles.

Speech recognition writes homophones - "caulk" comes back as a swear - and
nothing in the transcript says so. The subtitles often do: they were written
by a person who knew what was said. So each detection is compared with the
subtitle on screen at that moment.

This never changes what is muted. When unsure, the word is muted; a subtitle
that disagrees only marks the detection as worth a listen, with the line
quoted, so a wrong call is quick to find and fix with "That was wrong".

Subtitles are imperfect evidence. They are often censored ("f***"), trimmed,
or timed for a different release. So:

* a censored word, or any listed word in the line, counts as agreeing;
* a line close in time counts, not only one exactly on the word;
* if most detections in a file disagree, the subtitles probably belong to
  another cut, and they are set aside for that file rather than flagging
  everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from . import words

# How far either side of a word a subtitle line may start or end and still
# count as the line it was said in. Subtitles lead and trail speech.
WINDOW = 0.75

# Below this, Whisper itself was unsure of the word.
LOW_CONFIDENCE = 0.5

# With this many detections checked, and more than this share disagreeing,
# the subtitles are taken not to match the file.
MISMATCH_MIN = 5
MISMATCH_SHARE = 0.6

AGREES = "agrees"
DIFFERS = "differs"

_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")
_TAG = re.compile(r"<[^>]+>|\{[^}]*\}")
# f***, s**t, sh*t, [bleep], ***: a swear the subtitler hid.
_CENSORED = re.compile(r"\b\w+[*#@$%]{2,}\w*|\w\*+\w|\[\s*(bleep|beep|censored|expletive)[^\]]*\]"
                       r"|\*{3,}", re.I)


@dataclass
class Cue:
    start: float
    end: float
    text: str


def _seconds(match: re.Match) -> float:
    h, m, s, ms = match.groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Cue]:
    """SRT to cues. Markup is dropped; malformed blocks are skipped."""
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\s*\r?\n", (text or "").strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            times = _TIME.findall(line)
            if "-->" in line and len(times) == 2:
                start, end = (_seconds(t) for t in _TIME.finditer(line))
                body = " ".join(_TAG.sub("", ln).strip() for ln in lines[i + 1:])
                if body:
                    cues.append(Cue(start, end, body))
                break
    return cues


def _says_it(text: str, word: str, matcher: words.Matcher) -> bool:
    """Whether a subtitle line carries this word, a hidden swear, or any listed word."""
    if _CENSORED.search(text):
        return True
    tokens = [{"word": t, "start": 0.0, "end": 0.0} for t in re.split(r"[\s\-—–/]+", text)]
    if any(words.normalize(t["word"]) == word for t in tokens):
        return True
    return bool(matcher.find(tokens))


def evidence(match: words.Match, cues: list[Cue], matcher: words.Matcher) -> tuple[str, str]:
    """(state, the subtitle line) for one detection; state is "" when no line is near."""
    near = [c for c in cues if c.start - WINDOW <= match.end and c.end + WINDOW >= match.start]
    if not near:
        return "", ""
    text = " ".join(c.text for c in near)
    word = words.normalize(match.text)
    return (AGREES if _says_it(text, word, matcher) else DIFFERS), text[:200]


def annotate(matches: list[words.Match], cues: list[Cue]) -> tuple[list[words.Match], str]:
    """Each match with its subtitle evidence, and a note if the subtitles were set aside.

    The comparison uses every built-in list whatever the household switched
    off: the question is whether the subtitle has a swear in it, not whether
    this household mutes that one.
    """
    if not cues or not matches:
        return matches, ""
    matcher = words.Matcher(context_words=frozenset())
    judged = [(m, *evidence(m, cues, matcher)) for m in matches]
    checked = [state for _m, state, _t in judged if state]
    differing = sum(1 for state in checked if state == DIFFERS)
    if len(checked) >= MISMATCH_MIN and differing / len(checked) > MISMATCH_SHARE:
        return matches, (f"subtitles set aside: {differing} of {len(checked)} lines "
                         f"disagreed, so they probably belong to another release")
    return [replace(m, subtitle=text, subtitle_state=state) for m, state, text in judged], ""


def worth_checking(match) -> bool:
    """Whether a detection is worth a listen: the subtitles say otherwise, or
    Whisper was unsure of the word. Works on a Match or a detection row."""
    state = match["subtitle_state"] if not hasattr(match, "subtitle_state") else match.subtitle_state
    sure = match["confidence"] if not hasattr(match, "confidence") else match.confidence
    return state == DIFFERS or (sure is not None and sure < LOW_CONFIDENCE)
