"""Checking detections against the file's own subtitles: evidence, and the
subtitle second opinion on uncertain words."""

from __future__ import annotations

import pytest

from cleanarr import subtitles, words

SRT = """1
00:00:00,500 --> 00:00:01,800
<i>What the fuck</i> is that?

2
00:00:02,900 --> 00:00:04,200
Get some caulk
on that seam.

3
00:00:10,000 --> 00:00:11,000
Oh, sh*t.

broken block without times

4
00:00:20,000 --> 00:00:21,000
{\\an8}Aw, [BLEEP] it.
"""


def m(text, start, end=None, confidence=None):
    return words.Match(start, end if end is not None else start + 0.3, text, "strong",
                       confidence=confidence)


def test_parse_srt():
    cues = subtitles.parse_srt(SRT)
    assert [(c.start, c.end) for c in cues] == [(0.5, 1.8), (2.9, 4.2), (10.0, 11.0), (20.0, 21.0)]
    assert cues[0].text == "What the fuck is that?"          # markup gone
    assert cues[1].text == "Get some caulk on that seam."     # lines joined
    assert cues[3].text == "Aw, [BLEEP] it."
    assert subtitles.parse_srt("") == []
    assert subtitles.parse_srt("1\n00:00:01.5 --> 00:00:02.25\nhi")[0].start == 1.5


@pytest.mark.parametrize("match, state", [
    (m("fuck,", 1.0), "agrees"),               # the same word
    (m("cock", 3.2), "differs"),               # the homophone case
    (m("shit", 10.2), "agrees"),               # censored in the subtitle
    (m("damn", 20.3), "agrees"),               # [BLEEP]
    (m("shit", 40.0), ""),                     # no line anywhere near
    (m("fucking", 4.6), "differs"),            # within the window of line 2
])
def test_evidence(match, state):
    cues = subtitles.parse_srt(SRT)
    got, text = subtitles.evidence(match, cues, words.Matcher())
    assert got == state
    if state:
        assert text


def test_any_listed_word_in_the_line_counts_as_agreeing():
    cues = [subtitles.Cue(0, 2, "This is bullshit.")]
    assert subtitles.evidence(m("shit", 0.5), cues, words.Matcher())[0] == "agrees"


def test_annotate_records_evidence_and_keeps_every_match():
    cues = subtitles.parse_srt(SRT)
    got, note = subtitles.annotate([m("fuck", 1.0), m("cock", 3.2)], cues)
    assert note == ""
    assert [(g.text, g.subtitle_state) for g in got] == [("fuck", "agrees"), ("cock", "differs")]
    assert "caulk" in got[1].subtitle


def test_subtitles_from_another_release_are_set_aside():
    cues = [subtitles.Cue(t, t + 1, "Nothing rude at all.") for t in range(0, 60, 2)]
    matches = [m("shit", t + 0.2) for t in range(0, 12, 2)]
    got, note = subtitles.annotate(matches, cues)
    assert "another release" in note
    assert all(g.subtitle_state == "" for g in got)
    assert got == matches


def test_no_subtitles_changes_nothing():
    matches = [m("shit", 1.0)]
    assert subtitles.annotate(matches, []) == (matches, "")


def test_worth_checking():
    assert subtitles.worth_checking(words.Match(0, 1, "x", "c", subtitle_state="differs"))
    assert subtitles.worth_checking(m("x", 0, confidence=0.3))
    assert not subtitles.worth_checking(m("x", 0, confidence=0.9))
    assert not subtitles.worth_checking(m("x", 0))
    assert subtitles.worth_checking({"subtitle_state": "", "confidence": 0.2})


def test_confidence_is_carried_from_the_transcript():
    got = words.Matcher().find([{"word": "shit", "start": 0, "end": 0.3, "probability": 0.42}])
    assert got[0].confidence == 0.42
    got = words.Matcher().find([{"word": "oh", "start": 0, "end": 0.1},
                                {"word": "my", "start": 0.1, "end": 0.2},
                                {"word": "god", "start": 0.2, "end": 0.4, "probability": 0.8}])
    assert got[0].confidence == 0.8
    assert words.Matcher().find([{"word": "shit", "start": 0, "end": 1}])[0].confidence is None


def test_webvtt_times_without_hours():
    vtt = "WEBVTT\n\n01:02.500 --> 01:04.000 align:start\nGet some caulk.\n"
    assert [(c.start, c.end, c.text) for c in subtitles.parse_srt(vtt)] == [(62.5, 64.0, "Get some caulk.")]


def decided(matches, checked=frozenset({"cock"})):
    marked, note = subtitles.annotate(matches, subtitles.parse_srt(SRT))
    assert note == ""
    return subtitles.decide(marked, set(checked))


def test_the_subtitles_decide_a_word_on_the_check_list():
    muted, left, still_open = decided([m("cock", 3.0)])
    assert (muted, still_open) == ([], [])
    assert left[0].needs_review and "caulk" in left[0].reason


def test_the_subtitles_decide_a_word_whisper_was_unsure_of():
    muted, left, _ = decided([m("fuck", 1.0, confidence=0.3), m("dick", 3.0, confidence=0.2)])
    assert [x.text for x in muted] == ["fuck"] and "subtitles agree" in muted[0].reason
    assert [x.text for x in left] == ["dick"]


def test_a_sure_word_is_never_unmuted_by_the_subtitles():
    # Subtitles soften swears far more often than Whisper invents them.
    muted, left, still_open = decided([m("dick", 3.0, confidence=0.95)])
    assert (muted, left) == ([], [])
    assert [x.text for x in still_open] == ["dick"]


def test_no_line_nearby_leaves_the_word_open():
    muted, left, still_open = decided([m("cock", 40.0)])
    assert (muted, left) == ([], [])
    assert [x.text for x in still_open] == ["cock"]


def test_a_bleeped_line_agrees():
    muted, left, _ = decided([m("cock", 20.2)])
    assert [x.text for x in muted] == ["cock"] and left == []


# ---------------------------------------------------------------- lining up another copy

LINES = ["Nobody leaves this house tonight", "Where did you put the money",
         "Somebody called the police already", "Get the truck around back now",
         "Those windows were locked yesterday", "Listen carefully before answering",
         "Nothing about this makes sense", "Grandma never trusted strangers",
         "Pack everything into the basement", "Morning comes faster than planned"]


def heard_and_cues(shift: float):
    """Words heard at their true times, and lines shown `shift` seconds late."""
    heard, cues = [], []
    for n in range(60):
        text = LINES[n % len(LINES)] + f" number{n}"
        start = 10.0 + n * 7.0
        for k, word in enumerate(text.split()):
            heard.append({"word": " " + word, "start": start + k * 0.4, "end": start + k * 0.4 + 0.3})
        cues.append(subtitles.Cue(start + shift - 0.2, start + shift + 3.0, text))
    return heard, cues


def test_subtitles_from_another_cut_are_moved_into_line():
    heard, cues = heard_and_cues(-8.5)
    moved, low, high = subtitles.align(cues, heard)
    assert abs(low + 8.5) <= 0.5 and abs(high + 8.5) <= 0.5
    assert abs(moved[0].start - cues[0].start + low) < 1e-9
    assert subtitles.align(moved, heard)[1:] == (0.0, 0.0)   # and they stay put


def test_subtitles_that_drift_are_moved_window_by_window():
    """Adverts cut differently: 2 s out at first, 12 s out after a break."""
    heard, cues = heard_and_cues(0.0)
    late = [subtitles.Cue(c.start + (2.0 if c.start < 220 else 12.0),
                          c.end + (2.0 if c.start < 220 else 12.0), c.text) for c in cues]
    moved, low, high = subtitles.align(late, heard)
    assert abs(low - 2.0) <= 0.5 and abs(high - 12.0) <= 0.5
    # Every line back within a fraction of its time on screen, the ones beside
    # the break included.
    assert max(abs(m.start - c.start) for m, c in zip(moved, cues)) <= 0.75


@pytest.mark.parametrize("speed", [23.976 / 25, 25 / 23.976])
def test_subtitles_timed_for_another_frame_rate_are_lined_up(speed):
    """A PAL release against a 23.976 fps file: 4% out, growing all the way."""
    heard, cues = heard_and_cues(0.0)
    stretched = [subtitles.Cue(c.start * speed + 3.0, c.end * speed + 3.0, c.text) for c in cues]
    moved, _low, _high = subtitles.align(stretched, heard)
    assert max(abs(m.start - c.start) for m, c in zip(moved, cues)) <= 0.75


def test_subtitles_that_drift_at_every_break_are_not_given_up_on():
    """Three different offsets: no single shift fits the whole file."""
    heard, cues = heard_and_cues(0.0)
    third = cues[-1].end / 3
    late = [subtitles.Cue(c.start + d, c.end + d, c.text) for c in cues
            for d in [2.0 if c.start < third else 6.0 if c.start < 2 * third else 11.0]]
    moved, low, high = subtitles.align(late, heard)
    assert max(abs(m.start - c.start) for m, c in zip(moved, cues)) <= 0.75


def test_subtitles_already_in_line_are_left_alone():
    heard, cues = heard_and_cues(0.0)
    moved, low, high = subtitles.align(cues, heard)
    assert (low, high) == (0.0, 0.0) and moved == cues


def test_subtitles_of_something_else_are_not_forced_into_line():
    heard, cues = heard_and_cues(0.0)
    other = [subtitles.Cue(c.start, c.end, "Completely unrelated dialogue here") for c in cues]
    assert subtitles.align(other, heard) == (other, 0.0, 0.0)
    assert subtitles.align([], heard) == ([], 0.0, 0.0)


def test_subtitles_that_do_not_follow_the_dialogue_are_rejected():
    heard, cues = heard_and_cues(0.0)
    assert subtitles.fits(cues, heard)
    other = [subtitles.Cue(c.start, c.end, "Completely unrelated dialogue here") for c in cues]
    assert not subtitles.fits(other, heard)
    # Too little heard to tell: no verdict, so the old per-detection check decides.
    assert subtitles.fits(other, heard[:30])
    assert subtitles.fits([], heard)



# ---------------------------------------------------------------- reading the script

def said(text: str, start: float = 10.0, gap: float = 0.35) -> list[dict]:
    return [{"word": " " + w, "start": start + n * gap, "end": start + n * gap + 0.3}
            for n, w in enumerate(text.split())]


def line_at(heard, text, start, end):
    return [subtitles.Cue(start, end, text)]


def detection(heard, word):
    w = next(h for h in heard if words.normalize(h["word"]) == word)
    return words.Match(start=w["start"], end=w["end"], text=w["word"].strip(),
                       category="strong", confidence=0.4)


SCRIPT_CASES = [
    # heard, subtitle line, detected word, expected
    ("you have to open a shit ton of bank accounts", "and open a ton of bank accounts", "shit", "omits"),
    ("it was no way in hell this is random", "No way this is random.", "hell", "omits"),
    ("pass me the cock gun right now please", "Pass me the caulk gun right now, please.", "cock", "differs"),
    ("we are here kill this motherfucker right away", "-We are here! -Kill this mother--", "motherfucker", "agrees"),
    ("you know on your period or some shit but anyway", "You know, on your period or...", "shit", "omits"),
    ("look at that holy shit it is huge", "Look at that. Holy shit, it is huge!", "shit", "agrees"),
]


@pytest.mark.parametrize("heard_text, line, word, want", SCRIPT_CASES)
def test_the_script_around_a_word_decides(heard_text, line, word, want):
    heard = said(heard_text)
    # The line is on screen well before the word, beyond the quick check.
    cues = line_at(heard, line, 8.0, 9.0) if want != "agrees" else line_at(heard, line, 9.5, 14.0)
    m = detection(heard, word)
    state, _shown = subtitles.evidence(m, cues, words.Matcher(context_words=frozenset()), heard)
    assert state == want


def test_a_line_that_leaves_the_word_out_is_no_reason_to_unmute():
    heard = said("you have to open a shit ton of bank accounts")
    m = detection(heard, "shit")
    marked, note = subtitles.annotate([m], line_at(heard, "and open a ton of bank accounts", 10.0, 14.0), heard)
    assert marked[0].subtitle_state == "omits" and note == ""
    muted, left, still_open = subtitles.decide(marked, set())
    assert (muted, left) == ([], []) and still_open == marked       # the model or muting decides
