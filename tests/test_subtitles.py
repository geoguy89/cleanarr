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
