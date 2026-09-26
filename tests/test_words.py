"""What gets silenced, and more importantly what does not."""

from __future__ import annotations

import pytest

from cleanarr.words import NEVER, Matcher, normalize, to_spans


def words(sentence: str, start: float = 0.0, step: float = 0.4) -> list[dict]:
    """A sentence as the ASR would hand it over: one word, one timestamp."""
    out = []
    t = start
    for w in sentence.split():
        out.append({"word": w, "start": round(t, 2), "end": round(t + step * 0.8, 2)})
        t += step
    return out


def censored(sentence: str, **kw) -> list[str]:
    return [hit.text.strip().lower().strip(",.!?")
            for hit in Matcher(**kw).find(words(sentence))]


@pytest.mark.parametrize("sentence, want", [
    ("what the fuck is that", ["fuck"]),
    ("he is fucking useless", ["fucking"]),
    ("that is bullshit and you know it", ["bullshit"]),
    ("Shit! That hurt.", ["shit"]),
    ("shit that is a damn lie", ["shit", "damn"]),
    ("this is a damn mess", ["damn"]),
    ("he called him a faggot", ["faggot"]),
    ("two shits given", ["shits"]),
    ("the bitch's car", ["bitch's"]),
])
def test_listed_words_are_caught(sentence, want):
    assert censored(sentence) == want


@pytest.mark.parametrize("phrase", [
    "she is in my class", "an assassin in the grass", "pass me the glasses",
    "the cockpit door", "a cocktail party", "shall we assume the assignment",
    "hello there", "the title of the document", "scrap that idea",
    "bass guitar", "cucumber sandwiches", "massive damage", "a bloody nose",
    "he screwed in the bulb", "damning evidence", "a cocky grin",
    "a cocker spaniel", "a booby trap",
])
def test_innocent_words_are_left_alone(phrase):
    assert censored(phrase) == []


def test_categories_switch_independently():
    assert censored("this damn shit", categories=("strong",)) == ["shit"]
    assert censored("this damn shit", categories=("mild",)) == ["damn"]
    assert censored("this damn shit", categories=()) == []


@pytest.mark.parametrize("sentence, want", [
    ("oh my god that is huge", ["god"]),
    ("for the love of god stop", ["god"]),
    ("holy christ look at that", ["christ"]),
    ("i swear to god i did", ["god"]),
    ("for god's sake listen", ["god's"]),
    ("god damn it all", ["god", "damn"]),
    ("the goddamn car", ["goddamn"]),
    ("christ that was close", ["christ"]),
    ("jesus h christ", ["jesus", "christ"]),
    ("we pray in the name of jesus christ amen", []),
    ("praise god for his mercy", []),
    ("the gospel says jesus christ is lord", []),
    ("thank god you are safe", []),
])
def test_the_lords_name(sentence, want):
    assert censored(sentence) == want


def test_blasphemy_off_leaves_it_entirely():
    assert censored("oh my god", categories=("strong", "mild")) == []


def test_bare_name_is_muted_even_in_prayer_when_not_checked_in_context():
    # Taking "jesus" off the check-in-context list means "always mute it".
    assert censored("we pray to jesus", context_words=frozenset()) == ["jesus"]


def test_household_exceptions():
    assert censored("pass the crab dip", never=frozenset({"crab"})) == []
    assert censored("what a muppet", extra=("muppet",)) == ["muppet"]
    # A never-word beats a custom word: the household said both, and "never"
    # is the one that was added to correct a mistake.
    assert censored("what a muppet", extra=("muppet",),
                    never=frozenset({"muppet"})) == []


def test_never_list_is_normalised():
    assert censored("oh shit", never=frozenset({"Shit"})) == []


def test_review_flags_and_reasons():
    m = Matcher()
    flags = {h.text.lower(): h.needs_review
             for h in m.find(words("oh my god that fucking hurts"))}
    assert flags == {"god": True, "fucking": False}
    reasons = {h.text.lower(): h.reason for h in m.find(words("oh my god that hurts"))}
    assert "oh my god" in reasons["god"]


def test_normalize():
    assert normalize("Fucking,") == "fucking"
    assert normalize("“God’s") == "god's"
    assert normalize("  ") == ""
    assert normalize(None) == ""


def test_empty_and_blank_tokens():
    assert Matcher().find([]) == []
    assert Matcher().find([{"word": "", "start": 0, "end": 0.1},
                           {"word": "...", "start": 0.1, "end": 0.2}]) == []


def test_default_never_list_includes_the_known_false_hits():
    for word in ("class", "cockpit", "cucumber", "title", "damage"):
        assert word in NEVER


# ---------------------------------------------------------------- spans

def test_phrase_span_starts_at_the_swear():
    spans = to_spans(Matcher().find(words("oh my god that is huge")), 0.12, 0.12)
    assert len(spans) == 1
    # "god" starts at 0.8; "my" ends at 0.72. The span must not reach "my".
    assert spans[0][0] > 0.6


def test_adjacent_swears_merge():
    assert len(to_spans(Matcher().find(words("god damn it all")), 0.12, 0.12)) == 1


def test_near_words_merge_and_far_words_do_not():
    assert to_spans(Matcher().find(words("shit shit", step=0.4)), 0.12, 0.12) == [(0.0, 0.84)]
    far = [{"word": "shit", "start": 1.0, "end": 1.3},
           {"word": "shit", "start": 10.0, "end": 10.3}]
    assert to_spans(Matcher().find(far), 0.1, 0.1) == [(0.9, 1.4), (9.9, 10.4)]


def test_padding_never_goes_below_zero():
    hit = Matcher().find([{"word": "shit", "start": 0.05, "end": 0.3}])
    assert to_spans(hit, 0.2, 0.2) == [(0.0, 0.5)]


def test_gap_threshold():
    a = [{"word": "shit", "start": 1.0, "end": 1.2},
         {"word": "shit", "start": 1.31, "end": 1.5}]
    # 1.2 -> 1.31 is inside the default 0.12 gap: merged.
    assert to_spans(Matcher().find(a), 0.0, 0.0) == [(1.0, 1.5)]
    b = [{"word": "shit", "start": 1.0, "end": 1.2},
         {"word": "shit", "start": 1.34, "end": 1.5}]
    assert to_spans(Matcher().find(b), 0.0, 0.0) == [(1.0, 1.2), (1.34, 1.5)]


def test_a_word_with_end_before_start_still_gets_a_span():
    hit = Matcher().find([{"word": "shit", "start": 2.0, "end": 1.9}])
    start, end = to_spans(hit, 0.0, 0.0)[0]
    assert end >= start


def test_spans_are_sorted_whatever_order_matches_arrive_in():
    hits = Matcher().find([{"word": "shit", "start": 5.0, "end": 5.2},
                           {"word": "fuck", "start": 1.0, "end": 1.2}])
    assert to_spans(hits, 0.0, 0.0) == [(1.0, 1.2), (5.0, 5.2)]


def test_never_list_applies_inside_phrases():
    assert censored("oh my god that is huge", never=frozenset({"god"})) == []
    assert censored("god damn it", never=frozenset({"god"})) == ["damn"]
    assert censored("jesus christ", never=frozenset({"Jesus"})) == ["christ"]
    # The default never list does not touch any phrase word.
    assert censored("oh my god") == ["god"]
