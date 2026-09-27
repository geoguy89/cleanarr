"""Checking what Whisper heard against the file's own subtitles.

Speech recognition writes homophones - "caulk" comes back as a swear - and
nothing in the transcript says so. The subtitles often do: they were written
by a person who knew what was said. So each detection is compared with the
subtitle on screen at that moment.

By default this never changes what is muted. When unsure, the word is muted;
a subtitle that disagrees only marks the detection as worth a listen, with the
line quoted, so a wrong call is quick to find and fix with "That was wrong".

With the subtitles switched on as a second opinion (see decide), they also
rule on the uncertain detections: a word on the check-in-context list, or one
Whisper itself was unsure of. A line that agrees keeps it muted without asking
anyone else; a line that says something else leaves it in, marked for a
listen; no line near it leaves the word to the model, or muted.

Subtitles are imperfect evidence. They are often censored ("f***"), trimmed,
or timed for a different release. So:

* a censored word, or any listed word in the line, counts as agreeing;
* a line close in time counts, not only one exactly on the word;
* if most detections in a file disagree, the subtitles probably belong to
  another cut, and they are set aside for that file rather than flagging
  everything.
"""

from __future__ import annotations

import bisect
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

# Hours are optional: WebVTT may write 01:02.500.
_TIME = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{1,3})")
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
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Cue]:
    """SRT (or WebVTT) to cues. Markup is dropped; malformed blocks are skipped."""
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


# Lining up subtitles borrowed from another copy of a film. A WEB-DL and a
# WEBRip of the same film can differ by a studio logo or two at the start, which
# shifts every line by the same few seconds. Measured on Trap House (2025): 3%
# of the transcript's words matched the other copy's subtitles as they were,
# 79% at -8.5 s, and 2% for subtitles of the wrong film at any shift.
ALIGN_LIMIT = 180.0        # seconds either way
ALIGN_MIN_SHARE = 0.4      # of sampled words found in the line at the new time
ALIGN_MIN_GAIN = 0.15      # over leaving them where they are
_WORD = re.compile(r"[a-z']+")


def _share(cues: list[Cue], starts: list[float], vocab: list[set[str]],
           sample: list[tuple[float, str]], offset: float) -> float:
    """Share of sampled words that a line on screen at (their time + offset) contains."""
    hit = 0
    for at, word in sample:
        at += offset
        i = bisect.bisect_right(starts, at + 1.0) - 1
        while i >= 0 and cues[i].end >= at - 1.0:
            if word in vocab[i]:
                hit += 1
                break
            i -= 1
    return hit / len(sample)


def align(cues: list[Cue], heard: list[dict]) -> tuple[list[Cue], float]:
    """(the cues, moved to line up with what was heard; the shift in seconds).

    The shift is kept only when it clearly helps - otherwise the cues come back
    unmoved, and if they belong to another cut entirely the mismatch check in
    annotate() sets them aside as before. One shift for the whole file: a copy
    that drifts is not lined up, and is then set aside the same way.
    """
    ordered = sorted(cues, key=lambda c: c.start)
    sample = [(float(w.get("start") or 0.0), words.normalize(w.get("word", "")))
              for w in heard]
    sample = [(t, w) for t, w in sample if len(w) >= 4][::2][:3000]
    if not ordered or not sample:
        return cues, 0.0
    starts = [c.start for c in ordered]
    vocab = [set(_WORD.findall(c.text.lower())) for c in ordered]
    share = lambda off: _share(ordered, starts, vocab, sample, off)  # noqa: E731
    here = share(0.0)
    _best, rough = max((share(float(k)), float(k))
                       for k in range(-int(ALIGN_LIMIT), int(ALIGN_LIMIT) + 1))
    # Lines stay on screen for seconds, so a run of neighbouring shifts scores
    # about the same; the middle of that run is the one that lines up, not
    # whichever end max() happens to land on.
    fine = [(rough + d / 4, share(rough + d / 4)) for d in range(-12, 13)]
    best = max(v for _o, v in fine)
    top = [o for o, v in fine if v >= best * 0.98]
    offset = round((min(top) + max(top)) / 2 * 4) / 4
    if best < ALIGN_MIN_SHARE or best - here < ALIGN_MIN_GAIN:
        return cues, 0.0
    # The cues are moved onto the transcript's clock: a word heard at t was
    # shown at t + offset, so a line shown at s belongs at s - offset.
    return [Cue(c.start - offset, c.end - offset, c.text) for c in cues], offset


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


def uncertain(match: words.Match, checked: set[str]) -> bool:
    """Whether a second opinion may rule on this detection: the word is on
    the check-in-context list, or Whisper was unsure of it."""
    word = words.normalize(match.text)
    sure = match.confidence
    return word in checked or (sure is not None and sure < LOW_CONFIDENCE)


def decide(matches: list[words.Match], checked: set[str]
           ) -> tuple[list[words.Match], list[words.Match], list[words.Match]]:
    """(muted on the subtitles' word, left in on it, still open).

    Works on matches annotate() has already marked. Only uncertain detections
    are ruled on: a word Whisper was sure of, and not on the check list, is
    muted whatever the subtitles say, because subtitles soften swears far more
    often than Whisper invents them. Everything not ruled on is still open,
    for the model or for muting.
    """
    muted, left, still_open = [], [], []
    for m in matches:
        if not uncertain(m, checked) or not m.subtitle_state:
            still_open.append(m)
        elif m.subtitle_state == AGREES:
            muted.append(replace(m, reason=m.reason or "uncertain word, the subtitles agree"))
        else:
            left.append(replace(m, needs_review=True,
                                reason=f"left in: the subtitles say “{m.subtitle}”"))
    return muted, left, still_open
