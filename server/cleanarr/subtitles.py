"""Checking what Whisper heard against the file's own subtitles.

Speech recognition writes homophones - "caulk" comes back as a swear - and
nothing in the transcript says so. The subtitles often do: they were written
by a person who knew what was said. So each detection is compared with the
subtitle on screen at that moment.

With the subtitle second opinion switched off, this never changes what is
muted: a subtitle that disagrees only marks the detection as worth a listen,
with the line quoted, so a wrong call is quick to find and fix with "That was
wrong".

With it on - the default - the subtitles also rule on the uncertain
detections (see decide): a word on the check-in-context list, or one
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
# The subtitles run straight past where the word was said - "open a ton of"
# for "open a shit-ton of". Subtitles soften swears all the time, so this is
# no evidence either way: the word is treated as if no line were near.
OMITS = "omits"

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


# Lining up borrowed subtitles - another copy's, or ones the media server
# fetched. Two kinds of difference, both measured:
#   * a steady shift: a WEB-DL and a WEBRip of Trap House (2025) differ by a
#     studio logo, so every line is 8.5 s out. 3% of the transcript's words
#     matched as they were, 79% shifted, 2% for the wrong film at any shift.
#   * drift: an HDTV recording of Line of Fire S01E01 against subtitles made
#     for a release with the adverts cut differently - 2 s out at the start,
#     13 s by the end, stepping at each break. One shift for the file put 32%
#     of words in the right line; a shift per three-minute window put 91%.
# So a shift is found for the whole file first, then refined window by window.
ALIGN_LIMIT = 180.0        # seconds either way, for the whole file
ALIGN_MIN_SHARE = 0.4      # of sampled words found in the line at the new time
ALIGN_MIN_GAIN = 0.15      # over leaving them where they are
# ...or this little, once the result fits well: a file's own track that is a
# touch loose. The Bear S01E02's put lines up to 0.8 s off the speech; lining
# it up took words in the right line from 81% to 86% and fixed a verdict.
ALIGN_GOOD_FIT = 0.6
ALIGN_SMALL_GAIN = 0.03
ALIGN_SPAN = 180.0         # seconds of speech per window
ALIGN_STEP = 60.0          # between window centres
ALIGN_REACH = 20.0         # how far a window may move from the whole-file shift
ALIGN_MIN_WORDS = 25       # fewer than this in a window: use the whole-file shift
# Subtitles timed for another frame rate run fast or slow throughout: a PAL
# release (25 fps) against a 23.976 fps file is 4% out, three and a half
# minutes over a film. No shift can fix that, so each common ratio is tried.
# In an audit of 8 episodes, every such case was set aside until this was
# added. 1.0 is kept unless another ratio does clearly better.
ALIGN_SPEEDS = (1.0, 23.976 / 25, 25 / 23.976, 24 / 25, 25 / 24, 23.976 / 24, 24 / 23.976)
ALIGN_SPEED_MARGIN = 0.05
_WORD = re.compile(r"[a-z']+")


def _share(cues: list[Cue], starts: list[float], vocab: list[set[str]],
           sample: list[tuple[float, str]], offset: float, slack: float = 1.0) -> float:
    """Share of sampled words that a line on screen at (their time + offset),
    give or take `slack` seconds, contains."""
    if not sample:
        return 0.0
    hit = 0
    for at, word in sample:
        at += offset
        i = bisect.bisect_right(starts, at + slack) - 1
        while i >= 0 and cues[i].end >= at - slack:
            if word in vocab[i]:
                hit += 1
                break
            i -= 1
    return hit / len(sample)


def _middle(scored: list[tuple[float, float]]) -> tuple[float, float]:
    """(shift, its score): the middle of the best-scoring run of shifts.

    A line stays on screen for seconds, so neighbouring shifts score about the
    same; the middle of that run is the one that lines up, not whichever end
    max() happens to land on."""
    best = max(v for _o, v in scored)
    top = [o for o, v in scored if v >= best * 0.98]
    return round((min(top) + max(top)) / 2 * 4) / 4, best


def align(cues: list[Cue], heard: list[dict]) -> tuple[list[Cue], float, float]:
    """(the cues moved to line up with what was heard; smallest and largest
    shift applied, in seconds - both 0 when they were left alone).

    Each common frame-rate ratio is tried (ALIGN_SPEEDS), then a shift for the
    whole file, then a shift per window. The result is kept only when, in the
    end, it clearly fits better than leaving the cues alone - otherwise they
    come back unmoved, and if they belong to another cut entirely fits() sets
    them aside. Judged on the end result rather than the whole-file shift
    alone: subtitles that drift at every advert break fit no single shift well,
    and an audit found two of four such episodes given up on that way.
    """
    if not cues:
        return cues, 0.0, 0.0
    here = _fit_share(cues, heard)
    here_tight = _fit_share(cues, heard, 0.3)
    best = None
    for speed in ALIGN_SPEEDS:
        timed = cues if speed == 1.0 else [Cue(c.start / speed, c.end / speed, c.text)
                                            for c in cues]
        moved = _align_at_speed(timed, heard)
        if moved is None:
            continue
        score = _fit_share(moved, heard)
        # A clear gain, or - once it fits well - a small one measured tightly,
        # where a line a fraction of a second off shows.
        clear = score >= ALIGN_MIN_SHARE and score - here >= ALIGN_MIN_GAIN
        tighter = (score >= ALIGN_GOOD_FIT
                   and _fit_share(moved, heard, 0.3) - here_tight >= ALIGN_SMALL_GAIN)
        if not (clear or tighter):
            continue
        if best is None or score > best[0] + (ALIGN_SPEED_MARGIN if best[2] == 1.0 else 0.0):
            best = (score, moved, speed)
    if best is None:
        return cues, 0.0, 0.0
    moved = best[1]
    shifts = [round(c.start - m.start, 2) for c, m in zip(cues, moved)]
    return moved, min(shifts), max(shifts)


def _fit_share(cues: list[Cue], heard: list[dict], slack: float = 1.0) -> float:
    """Share of sampled heard words inside a line that contains them, as they
    stand, give or take `slack` seconds."""
    ordered = sorted(cues, key=lambda c: c.start)
    sample = [(float(w.get("start") or 0.0), words.normalize(w.get("word", ""))) for w in heard]
    sample = [(t, w) for t, w in sample if len(w) >= 4][::2][:3000]
    if not ordered or not sample:
        return 0.0
    vocab = [set(_WORD.findall(c.text.lower())) for c in ordered]
    return _share(ordered, [c.start for c in ordered], vocab, sample, 0.0, slack)


def _align_at_speed(cues: list[Cue], heard: list[dict]) -> list[Cue] | None:
    """The cues moved by a whole-file shift and then window by window, in the
    same order as given; None when there is nothing to line up with."""
    ordered = sorted(cues, key=lambda c: c.start)
    words_heard = [(float(w.get("start") or 0.0), words.normalize(w.get("word", "")))
                   for w in heard]
    words_heard = [(t, w) for t, w in words_heard if len(w) >= 4]
    sample = words_heard[::2][:3000]
    if not ordered or not sample:
        return None
    starts = [c.start for c in ordered]
    vocab = [set(_WORD.findall(c.text.lower())) for c in ordered]

    def share(words_, off, slack=1.0):
        return _share(ordered, starts, vocab, words_, off, slack)

    _best, rough = max((share(sample, float(k)), float(k))
                       for k in range(-int(ALIGN_LIMIT), int(ALIGN_LIMIT) + 1))
    offset, _best = _middle([(rough + d / 4, share(sample, rough + d / 4))
                             for d in range(-12, 13)])

    # Every word heard, short ones included, for finding where a line starts.
    spoken = sorted((float(w.get("start") or 0.0), words.normalize(w.get("word", "")))
                    for w in heard)
    spoken_at = [t for t, _w in spoken]

    def settle(local: float, lo: float, hi: float) -> float:
        """`local` corrected so each line starts where its first word is heard.

        The best-fit run above is found from where words fall inside lines,
        and a line stays on screen after its words end - reading time - so the
        middle of that run sits about half a second late. Where a line begins
        is what a subtitler times to, so the typical gap between the start of a
        line and its first word heard is taken off. Needs three lines that
        agree on their first word; moves at most a second."""
        gaps = []
        for cue in ordered:
            at = cue.start - local
            if not lo <= at < hi:
                continue
            first = _WORD.findall(cue.text.lower())
            if not first:
                continue
            a = bisect.bisect_left(spoken_at, at - 1.0)
            b = bisect.bisect_right(spoken_at, at + 1.0)
            near = [t - at for t, w in spoken[a:b] if w == first[0]]
            if near:
                gaps.append(min(near, key=abs))
        if len(gaps) < 3:
            return local
        gaps.sort()
        middle = gaps[len(gaps) // 2]
        return round(local - max(-1.0, min(1.0, middle)), 2)

    # Window by window, on the transcript's clock, with a tighter slack: here
    # the question is which line, not which film.
    end = words_heard[-1][0] if words_heard else 0.0
    windows: list[tuple[float, float]] = []
    centre = ALIGN_SPAN / 2
    while centre - ALIGN_SPAN / 2 <= end:
        chunk = [(t, w) for t, w in words_heard
                 if centre - ALIGN_SPAN / 2 <= t < centre + ALIGN_SPAN / 2]
        local = offset
        if len(chunk) >= ALIGN_MIN_WORDS:
            reach = int(ALIGN_REACH * 4)
            found, score = _middle([(offset + d / 4, share(chunk, offset + d / 4, 0.3))
                                    for d in range(-reach, reach + 1)])
            if score >= ALIGN_MIN_SHARE:
                local = found
        local = settle(local, centre - ALIGN_SPAN / 2, centre + ALIGN_SPAN / 2)
        windows.append((centre, local))
        centre += ALIGN_STEP

    # A word heard at t was shown at t + shift, so a line shown at s belongs
    # at s - shift. Each line takes the shift of the window it falls in - or,
    # next to an advert break where the shift steps, a neighbouring window's,
    # if that puts more of its own words where they were heard.
    centres = [c for c, _ in windows]
    times = [t for t, _ in words_heard]

    def fits(cue: Cue, shift: float) -> int:
        lo = bisect.bisect_left(times, cue.start - shift - 0.3)
        hi = bisect.bisect_right(times, cue.end - shift + 0.3)
        mine = set(_WORD.findall(cue.text.lower()))
        return sum(1 for _t, w in words_heard[lo:hi] if w in mine)

    moved = []
    for cue in cues:
        heard_at = cue.start - offset
        k = bisect.bisect_left(centres, heard_at)
        if k == len(centres) or (k > 0 and heard_at - centres[k - 1] < centres[k] - heard_at):
            k -= 1
        k = max(k, 0)
        shift = windows[k][1]
        for other in {windows[j][1] for j in (k - 1, k + 1) if 0 <= j < len(windows)} - {shift}:
            if fits(cue, other) > fits(cue, shift):
                shift = other
        moved.append(Cue(cue.start - shift, cue.end - shift, cue.text))
    return moved


# Whether a set of subtitles is of this dialogue at all. Measured after lining
# up: the right subtitles put 78-98% of heard words in the line on screen,
# another film's 2%. Checked against everything heard, not only the swears, so
# a file with two detections is protected as well as one with twenty.
FIT_MIN = 0.3
FIT_MIN_WORDS = 20          # fewer words heard than this: no verdict


def fits(cues: list[Cue], heard: list[dict]) -> bool:
    """False when enough was heard to tell, and the subtitles do not follow it -
    a commentary track, another episode, a language tagged wrongly."""
    ordered = sorted(cues, key=lambda c: c.start)
    sample = [(float(w.get("start") or 0.0), words.normalize(w.get("word", "")))
              for w in heard]
    sample = [(t, w) for t, w in sample if len(w) >= 4][::2][:3000]
    if not ordered or len(sample) < FIT_MIN_WORDS:
        return True
    vocab = [set(_WORD.findall(c.text.lower())) for c in ordered]
    return _share(ordered, [c.start for c in ordered], vocab, sample, 0.0) >= FIT_MIN


# Reading the script around a word. Asking only "does the line on screen have
# the word?" cannot tell a subtitle that leaves a swear out from one that says
# something else in its place, and the two mean opposite things: only the
# second is evidence Whisper misheard. So the words heard just before and after
# are found in the subtitles, and what sits between them is the answer. On
# Trap House (2025) this turned 5 "says something else" into "leaves it out"
# and 3 into "agrees", and changed none of the 84 that already agreed.
_ANCHOR_STOP = {"a", "an", "the", "i", "i'm", "im", "my", "me", "you", "your", "it", "its",
                "it's", "oh", "uh", "um", "and", "or", "but", "to", "of", "in", "on", "at",
                "is", "was", "be", "we", "he", "she", "they", "so", "no", "yes", "yeah", "ok",
                "okay", "up", "for", "that", "this", "just", "like", "what", "do", "don't", "not"}
SCRIPT_CONTEXT = 6.0       # seconds of subtitles either side of the word
SCRIPT_MAX_GAP = 6         # subtitle words allowed between the two anchors
WIDE_WINDOW = 2.5          # the nearby-line check, when nothing anchors
_SPLIT = re.compile(r"[\s\-—–/]+")
_CUT = re.compile(r"(--|—|…|\.\.\.)$")


def _script_tokens(cues: list[Cue], at: float) -> list[tuple[str, str]]:
    """(bare word, as written) for every subtitle word around `at`, in order."""
    out = []
    for c in sorted(cues, key=lambda c: c.start):
        if c.end >= at - SCRIPT_CONTEXT and c.start <= at + SCRIPT_CONTEXT:
            for raw in c.text.split():
                for part in _SPLIT.split(raw):
                    bare = words.normalize(part)
                    if bare:
                        out.append((bare, raw))
    return out


def _anchors(heard: list[str], i: int, step: int) -> list[tuple[tuple[str, ...], int]]:
    """Words to find on one side of heard[i], nearest first: a distinctive word
    on its own, or a pair of words for common ones. Each comes with how many
    heard words lie between it and the detection."""
    out = []
    for k in range(1, 4):
        j = i + step * k
        if not 0 <= j < len(heard):
            break
        w = heard[j]
        if w and w not in _ANCHOR_STOP and len(w) >= 3:
            out.append(((w,), k - 1))
        j2 = j + step
        if 0 <= j2 < len(heard) and heard[j2] and w:
            out.append(((heard[j2], w) if step < 0 else (w, heard[j2]), k - 1))
    return out


def _at(tokens: list[tuple[str, str]], seq: tuple[str, ...], start: int = 0, end: int = -1):
    end = len(tokens) if end < 0 else min(end, len(tokens))
    for p in range(start, end - len(seq) + 1):
        if tuple(t for t, _ in tokens[p:p + len(seq)]) == seq:
            yield p


def _in_the_script(match: words.Match, word: str, heard: list[dict], cues: list[Cue],
                   matcher: words.Matcher) -> str | None:
    """AGREES / OMITS / DIFFERS from what the subtitles have between the words
    heard either side, or None when neither side can be found."""
    bare = [words.normalize(h.get("word", "")) for h in heard]
    times = [float(h.get("start") or 0.0) for h in heard]
    k = bisect.bisect_left(times, match.start - 0.05)
    near = [j for j in range(max(0, k - 3), min(len(bare), k + 4))
            if bare[j] == word or (len(word) >= 4 and bare[j].startswith(word[:4]))]
    if not near:
        return None
    i = min(near, key=lambda j: abs(times[j] - match.start))
    tokens = _script_tokens(cues, match.start)
    if not tokens:
        return None

    def says(slot) -> bool:
        return any(t == word or _CENSORED.search(raw)
                   or (len(t) >= 4 and word.startswith(t) and _CUT.search(raw))
                   for t, raw in slot) or bool(matcher.find(
                       [{"word": raw, "start": 0.0, "end": 0.0} for _t, raw in slot]))

    for left, lgap in _anchors(bare, i, -1):
        for right, rgap in _anchors(bare, i, +1):
            between = set(bare[i - lgap:i] + bare[i + 1:i + 1 + rgap])
            outcomes = []
            for p in _at(tokens, left):
                a = p + len(left)
                found = False
                for q in _at(tokens, right, a, a + SCRIPT_MAX_GAP + len(right)):
                    found = True
                    slot = tokens[a:q]
                    rest = [t for t, _raw in slot if t not in between]
                    outcomes.append(AGREES if says(slot) else (OMITS if not rest else DIFFERS))
                    break
                # Nothing lines up after it, and the subtitles break off right
                # where the word was: "on your period or..."
                if not found and _CUT.search(tokens[a - 1][1]):
                    outcomes.append(OMITS)
            if outcomes:
                # Either reading will do when a phrase repeats: agreeing keeps
                # the word muted, which is where every doubt should land.
                return AGREES if AGREES in outcomes else (OMITS if OMITS in outcomes else DIFFERS)
    # A line cut off right on the word: "Kill this mother--".
    for left, _gap in _anchors(bare, i, -1):
        for p in _at(tokens, left):
            a = p + len(left)
            if a < len(tokens) and _CUT.search(tokens[a][1]) and len(tokens[a][0]) >= 4 \
                    and word.startswith(tokens[a][0]):
                return AGREES
    return None


def evidence(match: words.Match, cues: list[Cue], matcher: words.Matcher,
             heard: list[dict] | None = None) -> tuple[str, str]:
    """(state, the subtitle line) for one detection; state is "" when no line is near.

    A line on screen at that moment that has the word settles it. Otherwise
    the script around it is read (see _in_the_script), when the transcript is
    given; failing that, the lines a little either side are checked.
    """
    word = words.normalize(match.text)
    near = [c for c in cues if c.start - WINDOW <= match.end and c.end + WINDOW >= match.start]
    if near and _says_it(" ".join(c.text for c in near), word, matcher):
        return AGREES, " ".join(c.text for c in near)[:200]
    wide = [c for c in cues
            if c.start - WIDE_WINDOW <= match.end and c.end + WIDE_WINDOW >= match.start]
    shown = " ".join(c.text for c in (near or wide))[:200]
    if heard:
        state = _in_the_script(match, word, heard, cues, matcher)
        if state is not None:
            return state, shown
    if not wide:
        return "", ""
    # From a line a little further off only the word itself counts: the next
    # line's own swear ("What the fuck is that?") says nothing about this one.
    own = {words.normalize(t) for c in wide for t in _SPLIT.split(c.text)}
    return (AGREES if word in own else DIFFERS), shown


def annotate(matches: list[words.Match], cues: list[Cue],
             heard: list[dict] | None = None) -> tuple[list[words.Match], str]:
    """Each match with its subtitle evidence, and a note if the subtitles were set aside.

    The comparison uses every built-in list whatever the household switched
    off: the question is whether the subtitle has a swear in it, not whether
    this household mutes that one.
    """
    if not cues or not matches:
        return matches, ""
    matcher = words.Matcher(context_words=frozenset())
    judged = [(m, *evidence(m, cues, matcher, heard)) for m in matches]
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
        if not uncertain(m, checked) or m.subtitle_state in ("", OMITS):
            still_open.append(m)
        elif m.subtitle_state == AGREES:
            muted.append(replace(m, reason=m.reason or "uncertain word, the subtitles agree"))
        else:
            left.append(replace(m, needs_review=True,
                                reason=f"left in: the subtitles say “{m.subtitle}”"))
    return muted, left, still_open
