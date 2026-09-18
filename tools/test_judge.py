"""The second opinion: which words get asked about, and how answers are read.

Run: python tools/test_judge.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from cleanarr import judge  # noqa: E402

failures: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n       got:  {got!r}\n       want: {want!r}")
        failures.append(name)


print("which words are double-checked by default")
# Kept: a plausible other meaning that has actually turned up.
for word in ("Christ", "nuts", "spade", "tit", "prick", "shag"):
    check(f"asks about {word}", judge.is_ambiguous(word), True)
# Always profanity in practice, so never worth a minute of thinking each.
for word in ("ass", "bitch", "cock", "fuck", "fucking", "shit",
             "bastard", "pussy", "cunt", "cocksucker", "dick", "Dicks,"):
    check(f"never asks about {word}", judge.is_ambiguous(word), False)
# Dropped on evidence: 152 checks between them, none ever cleared.
for word in ("hell", "damn", "crap"):
    check(f"no longer asks about {word}", judge.is_ambiguous(word), False)

print("the list is a setting, not a rule")
# "cock" off the default list means the caulk scene would be muted again -
# which is why it has to be possible to put it back without touching code.
check("a household list replaces the default",
      judge.is_ambiguous("cock", {"cock", "caulk"}), True)
check("and removing a word from that list mutes it outright",
      judge.is_ambiguous("dick", {"cock"}), False)

print("context lines")
words = [{"word": w} for w in "you are gonna get some caulk and caulk that wall".split()]
check("marks the word being judged",
      judge.context_line(words, 5, span=2), "get some **caulk** and caulk")

print("reading the model's answer")
check("plain json",
      judge._parse('[{"n":1,"verdict":"CLEAN","instead":"caulk"},{"n":2,"verdict":"PROFANE"}]'),
      {1: ("CLEAN", "caulk"), 2: ("PROFANE", "")})
check("fenced json",
      judge._parse('```json\n[{"n":1,"verdict":"clean","instead":"cork"}]\n```'),
      {1: ("CLEAN", "cork")})
check("thinking tags are stripped",
      judge._parse('<think>hmm, caulk</think>[{"n":1,"verdict":"CLEAN","instead":"caulk"}]'),
      {1: ("CLEAN", "caulk")})
check("prose around the json",
      judge._parse('Here you go: [{"n":1,"verdict":"PROFANE"}] hope that helps'),
      {1: ("PROFANE", "")})
check("a clear that names no ordinary word is not trusted",
      judge._parse('[{"n":1,"verdict":"CLEAN"}]'), {1: ("PROFANE", "")})
check("reverent counts as naming one",
      judge._parse('[{"n":1,"verdict":"CLEAN","instead":"reverent"}]'),
      {1: ("CLEAN", "reverent")})
check("nonsense answers are dropped, not guessed",
      judge._parse('[{"n":1,"verdict":"MAYBE"},{"n":2,"verdict":"CLEAN","instead":"pass"}]'),
      {2: ("CLEAN", "pass")})
check("no json at all", judge._parse('I could not decide.'), {})
check("empty", judge._parse(''), {})

print("a missing Ollama does not silently clear words")
check("no url means no verdicts (so everything stays muted)",
      judge.adjudicate([{"n": 1, "line": "x"}], "", "qwen3.5:9b"), {})
check("unreachable host is handled the same way",
      judge.adjudicate([{"n": 1, "line": "x"}], "http://127.0.0.1:9", "qwen3.5:9b",
                       timeout=2.0), {})

print()
if failures:
    print(f"{len(failures)} failed: {', '.join(failures)}")
    sys.exit(1)
print("all judge checks passed")

# ---------------------------------------------------------------------------
print("grouping repeated words into one decision")


class FakeMatch:
    def __init__(self, text, start):
        self.text, self.start = text, start
        self.end = start + 0.3


transcript = [{"word": w, "start": i * 1.0} for i, w in enumerate(
    ("it was not properly dry walled and caulk but someone clogged the hole "
     "so I am gonna caulk that right now it takes five seconds to caulk").split())]
amb = [(0, FakeMatch("cocked", 6.0)), (1, FakeMatch("cock", 17.0)), (2, FakeMatch("cock.", 24.0))]
groups = judge.group(amb, transcript)
check("same word in one scene becomes one question", len(groups), 2)
check("and covers every occurrence",
      sorted(i for g in groups for i in g.indexes), [0, 1, 2])
far = [(0, FakeMatch("hell", 10.0)), (1, FakeMatch("hell", 4000.0))]
check("the same word an hour later is asked separately",
      len(judge.group(far, transcript)), 2)
check("a group's line quotes the context",
      "caulk" in judge.group([(0, FakeMatch("cock", 17.0))], transcript)[0].line, True)

if failures:
    print(f"\n{len(failures)} failed: {', '.join(failures)}")
    sys.exit(1)
print("grouping checks passed")
