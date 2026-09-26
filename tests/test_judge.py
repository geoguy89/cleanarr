"""The second opinion: which words get asked about, and how answers are read."""

from __future__ import annotations

import json

import pytest

from cleanarr import db, judge


@pytest.mark.parametrize("word", ["Christ", "nuts", "spade", "tit", "prick", "shag"])
def test_asks_about_words_with_another_meaning(word):
    assert judge.is_ambiguous(word)


@pytest.mark.parametrize("word", ["ass", "bitch", "cock", "fuck", "fucking", "shit",
                                  "bastard", "pussy", "cunt", "cocksucker", "dick",
                                  "Dicks,", "hell", "damn", "crap"])
def test_never_asks_about_words_that_are_always_profanity(word):
    assert not judge.is_ambiguous(word)


def test_the_list_is_a_setting():
    assert judge.is_ambiguous("cock", {"cock", "caulk"})
    assert not judge.is_ambiguous("dick", {"cock"})
    # An empty list means nothing is checked, not "use the default".
    assert not judge.is_ambiguous("christ", set())


def test_context_line_marks_the_word():
    ws = [{"word": w} for w in "you are gonna get some caulk and caulk that wall".split()]
    assert judge.context_line(ws, 5, span=2) == "get some **caulk** and caulk"


@pytest.mark.parametrize("raw, want", [
    ('[{"n":1,"verdict":"CLEAN","instead":"caulk"},{"n":2,"verdict":"PROFANE"}]',
     {1: ("CLEAN", "caulk"), 2: ("PROFANE", "")}),
    ('```json\n[{"n":1,"verdict":"clean","instead":"cork"}]\n```', {1: ("CLEAN", "cork")}),
    ('<think>hmm, caulk</think>[{"n":1,"verdict":"CLEAN","instead":"caulk"}]',
     {1: ("CLEAN", "caulk")}),
    ('Here you go: [{"n":1,"verdict":"PROFANE"}] hope that helps', {1: ("PROFANE", "")}),
    ('[{"n":1,"verdict":"CLEAN"}]', {1: ("PROFANE", "")}),
    ('[{"n":1,"verdict":"CLEAN","instead":"reverent"}]', {1: ("CLEAN", "reverent")}),
    ('[{"n":1,"verdict":"MAYBE"},{"n":2,"verdict":"CLEAN","instead":"pass"}]',
     {2: ("CLEAN", "pass")}),
    ('I could not decide.', {}),
    ('', {}),
    ('[not json]', {}),
    ('{"n":1}', {}),
])
def test_parse(raw, want):
    assert judge._parse(raw) == want


class FakeMatch:
    def __init__(self, text, start):
        self.text, self.start, self.end = text, start, start + 0.3


TRANSCRIPT = [{"word": w, "start": i * 1.0} for i, w in enumerate(
    ("it was not properly dry walled and caulk but someone clogged the hole "
     "so I am gonna caulk that right now it takes five seconds to caulk").split())]


def test_grouping():
    amb = [(0, FakeMatch("cocked", 6.0)), (1, FakeMatch("cock", 17.0)),
           (2, FakeMatch("cock.", 24.0))]
    groups = judge.group(amb, TRANSCRIPT)
    assert len(groups) == 2
    assert sorted(i for g in groups for i in g.indexes) == [0, 1, 2]
    far = [(0, FakeMatch("hell", 10.0)), (1, FakeMatch("hell", 4000.0))]
    assert len(judge.group(far, TRANSCRIPT)) == 2
    assert "caulk" in judge.group([(0, FakeMatch("cock", 17.0))], TRANSCRIPT)[0].line


# ---------------------------------------------------------------- talking to it

def test_no_url_means_no_verdicts():
    assert judge.adjudicate([{"n": 1, "line": "x"}], "", "qwen3.5:9b") == {}


def test_unreachable_host_means_no_verdicts(home):
    assert judge.adjudicate([{"n": 1, "line": "x"}], "http://127.0.0.1:9",
                            "qwen3.5:9b", timeout=2.0) == {}


def _ollama(stub, verdicts):
    """An Ollama stand-in answering each question from `verdicts` by n."""
    def generate(req):
        body = req.json()
        if body.get("prompt") == "":
            return 200, {"done": True}
        n = int(body["prompt"].rsplit("LINES:\n", 1)[1].split(".", 1)[0])
        return 200, {"response": json.dumps([{"n": n, **verdicts[n]}])}
    stub.route("POST", "/api/generate", generate)


def test_ollama_answers_are_used_and_remembered(home, stub):
    judge._style.clear()
    _ollama(stub, {1: {"verdict": "CLEAN", "instead": "caulk"},
                   2: {"verdict": "PROFANE", "instead": ""}})
    items = [{"n": 1, "line": 'the word "cock"\n   "…get some **cock**…"'},
             {"n": 2, "line": 'the word "prick"\n   "…you **prick**…"'}]
    seen = []
    out = judge.adjudicate(items, stub.url, "m", keep_alive="30s",
                           progress=lambda done, total, word: seen.append((done, total, word)))
    assert out == {1: ("CLEAN", "caulk"), 2: ("PROFANE", "")}
    assert seen == [(0, 2, "cock"), (1, 2, "prick")]
    sent = [r.json() for r in stub.seen("/api/generate")]
    assert [s["keep_alive"] for s in sent] == ["30s", "0s"]   # let go after the last
    assert sent[0]["options"]["num_thread"] == 2
    assert judge.api_style(stub.url) == "ollama"

    # Asked again: answered from the cache, nothing sent.
    before = len(stub.requests)
    assert judge.adjudicate(items, stub.url, "m") == out
    assert len(stub.requests) == before
    assert db.connect().execute(
        "SELECT word FROM judge_cache ORDER BY word").fetchall()[0]["word"] == "cock"


def test_openai_compatible_server_is_used_when_there_is_no_native_api(home, stub):
    judge._style.clear()
    stub.route("POST", "/api/generate", lambda req: (404, {"error": "nope"}))
    stub.route("POST", "/v1/chat/completions", {
        "choices": [{"message": {"content": '[{"n":1,"verdict":"CLEAN","instead":"cork"}]'}}]})
    out = judge.adjudicate([{"n": 1, "line": 'the word "cock"'}], stub.url, "m")
    assert out == {1: ("CLEAN", "cork")}
    assert judge.api_style(stub.url) == "openai"


def test_a_busy_ollama_is_not_mistaken_for_a_missing_one(home, stub):
    judge._style.clear()
    stub.route("POST", "/api/generate", lambda req: (500, {"error": "busy"}))
    stub.route("POST", "/v1/chat/completions", {
        "choices": [{"message": {"content": '[{"n":1,"verdict":"CLEAN","instead":"x"}]'}}]})
    assert judge.adjudicate([{"n": 1, "line": "x"}], stub.url, "m") == {}
    assert not stub.seen("/v1/chat/completions")


def test_max_questions_bounds_the_work(home, stub):
    judge._style.clear()
    _ollama(stub, {n: {"verdict": "PROFANE"} for n in range(1, 6)})
    items = [{"n": n, "line": f'the word "w{n}"'} for n in range(1, 6)]
    out = judge.adjudicate(items, stub.url, "m", max_questions=2)
    assert sorted(out) == [1, 2]


def test_release_vram_unloads_every_resident_model(stub):
    stub.route("GET", "/api/ps", {"models": [{"model": "a:1"}, {"name": "b:2"}]})
    stub.route("POST", "/api/generate", {"done": True})
    assert judge.release_vram(stub.url) == ["a:1", "b:2"]
    assert [r.json()["keep_alive"] for r in stub.seen("/api/generate")] == [0, 0]
    assert judge.release_vram("") == []
