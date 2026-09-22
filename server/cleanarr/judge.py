"""A second opinion on the words that are only sometimes profanity.

Whisper writes homophones. In one episode of The Bear a scene about drywall
produced nine mutes of "cock", because that is how "caulk" comes back from
speech recognition. Nothing about the word list can fix that: the word really
is on the list, and the sentence really does contain it.

What settles it is the sentence around the word, which is exactly the sort of
judgement a small local model is good at. So the ambiguous matches - and only
those, a handful per episode - are sent to Ollama with their context, and it
says whether each one was used as profanity.

Three rules keep this honest:

* Only AMBIGUOUS words are asked about. "fucking" is never sent anywhere.
* If Ollama is unreachable, or answers nonsense, the word stays muted. The
  service fails towards silence, which is the reason it exists.
* Every verdict is recorded and shown, so a wrong call can be seen and fixed
  rather than quietly changing what the family hears.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

import httpx

TIMEOUT = 120.0

PROMPT = """You decide whether words were used as profanity or insult.

Each numbered entry is one word, with one or more quotes of where it was said.
The word being judged is in **asterisks**. Judge that word's meaning in these
quotes. Other swearing in the same scene tells you nothing about this word -
people swear while talking about ordinary things.

Speech recognition wrote these quotes, so a similar-sounding ordinary word is
often what was really said. If an ordinary word fits the scene, say CLEAN. A
scene about drywall and joint compound is caulk, not a body part, however much
the characters are swearing.

Judge PROFANE if it is a swear word, an insult aimed at someone, crude sexual
slang, a slur, or God's name used as an exclamation or in anger.

Judge CLEAN if the sentence uses an ordinary meaning of the word or a
similar-sounding word, for example:
- caulk, cork or cooking heard as "cock"
- a donkey, or a word like "pass" or "class", heard as "ass"
- a female dog in a sentence about dogs
- a cat, a bird, or a person's name
- "hell" or "damn" inside a Bible reading, a prayer, a hymn or a sermon
- Jesus or Christ spoken reverently, in prayer, teaching or worship

If you answer CLEAN you must say, in "instead", what was really said - the
ordinary word you think the recognition misheard, or "reverent" for God's name
spoken in prayer, teaching or worship. If you cannot name one, it is PROFANE.

Reply with ONLY a JSON array, no other text:
[{"n": 1, "verdict": "CLEAN", "instead": "caulk"}]
or
[{"n": 1, "verdict": "PROFANE", "instead": ""}]

LINES:
"""


# Words whose sense really does depend on the sentence. Everything else is
# decided by the word list alone and never leaves the machine.
#
# This list was cut down on evidence, not taste. Across 196 checks against the
# real library the second opinion changed the outcome twice, and the words it
# never once cleared were the frequent ones:
#
#     hell 70 asked / 0 cleared      bitch 19 / 0
#     ass  41 / 0                    jesus, christ, "oh my god" 24 / 0
#     damn 22 / 0                    crap  1 / 1  (a wrong clear: "trash")
#
# At 30-100 seconds a question, that was most of the time a job took, spent to
# confirm what the word list already knew. Those words are now simply muted.
#
# What stays is where a plausible other meaning exists and has been seen. The
# religious words stay because getting those wrong is what most households
# mind most - and because words.py has already filtered out the plainly
# reverent ones before here, so only genuine exclamations reach the model.
#
# The unambiguous ones - ass, bitch, fuck, fucking, shit, bastard, pussy, cunt,
# cocksucker - are simply muted. They were never once cleared in testing, and
# asking costs a minute each.
#
# Worth knowing before emptying this list: "cock" is the founding caulk case,
# nine mutes in a scene about drywall. With it off, that scene is silenced
# again. It is a setting rather than a rule, so it can go back on in Settings.
DEFAULT_AMBIGUOUS = {
    # "dick" was here - it earned its place once by turning out to be "stick" -
    # but a blanket rule is easier to live with: if it is said, mute it. Put it
    # back in Settings if a show ever needs the distinction.
    "tit", "tits",                              # birds
    "prick", "pricks",                          # a pin prick
    "screw", "screwed",                         # a screw
    "balls", "nuts",                            # sport, food
    "shag",                                     # carpet, a dance, a seabird
    "faggot",                                   # British food, a bundle of sticks
    "coon", "spade",                            # raccoons and Maine Coons; cards, gardening
    "jesus", "christ",                          # reverent speech
}

# The live list, which Settings replaces.
AMBIGUOUS = set(DEFAULT_AMBIGUOUS)


def is_ambiguous(text: str, words: set[str] | None = None) -> bool:
    bare = re.sub(r"[^a-z']", "", str(text or "").lower())
    return bare in (words if words is not None else AMBIGUOUS)


def context_line(words: list[dict], index: int, span: int = 22) -> str:
    """The sentence around a match, with the word itself marked."""
    lo, hi = max(0, index - span), min(len(words), index + span + 1)
    out = []
    for i in range(lo, hi):
        word = str(words[i].get("word", "")).strip()
        out.append(f"**{word}**" if i == index else word)
    return " ".join(out).strip()


@dataclass
class Group:
    """One decision: a word, the matches it covers, and what to show the model."""
    n: int
    word: str
    indexes: tuple[int, ...]
    line: str


def group(ambiguous: list[tuple[int, object]], transcript: list[dict],
          window: float = 300.0, quotes: int = 4) -> list[Group]:
    """Cluster the same ambiguous word said near itself into one question.

    A word used repeatedly within `window` seconds is one situation - a scene
    about caulking, a sermon mentioning hell - so it gets one verdict from
    pooled quotes rather than a coin toss per occurrence.
    """
    starts = [w.get("start", 0.0) for w in transcript]

    def nearest(t: float) -> int:
        if not starts:
            return 0
        return min(range(len(starts)), key=lambda k: abs(starts[k] - t))

    buckets: dict[str, list[list[tuple[int, object]]]] = {}
    for index, match in ambiguous:
        key = re.sub(r"[^a-z']", "", str(match.text or "").lower())[:12]
        chains = buckets.setdefault(key, [])
        if chains and match.start - getattr(chains[-1][-1][1], "start", 0.0) <= window:
            chains[-1].append((index, match))
        else:
            chains.append([(index, match)])

    groups: list[Group] = []
    n = 0
    for word, chains in buckets.items():
        for chain in chains:
            n += 1
            sample = chain[:quotes] if len(chain) <= quotes else (
                [chain[0], chain[len(chain) // 3], chain[2 * len(chain) // 3], chain[-1]])
            lines = [context_line(transcript, nearest(m.start)) for _i, m in sample]
            heard = f'the word "{word}"'
            if len(chain) > 1:
                heard += f", heard {len(chain)} times in this scene"
            groups.append(Group(
                n=n, word=word, indexes=tuple(i for i, _m in chain),
                line=heard + "\n   " + "\n   ".join(f'"…{line}…"' for line in lines)))
    return groups


def release_vram(url: str, timeout: float = 20.0) -> list[str]:
    """Ask Ollama to hand back the card, and say what it let go of.

    Whoever last used Ollama decides how long its model stays resident, and
    that lease can be hours - the OCR watcher and the n8n flows both ask for
    one. When it is a 9B model on an 8GB card there is nothing left for
    Whisper, and no amount of waiting helps because the lease has not expired;
    it is simply longer than any job is willing to sit through.

    Evicting is cheap and safe: Ollama reloads the model from page cache in a
    few seconds the next time anything asks for it. Nothing is lost but that.
    """
    if not url:
        return []
    base = url.rstrip("/")
    try:
        resident = (httpx.get(f"{base}/api/ps", timeout=timeout).json()
                    .get("models") or [])
    except (httpx.HTTPError, ValueError):
        return []

    let_go = []
    for entry in resident:
        name = entry.get("model") or entry.get("name")
        if not name:
            continue
        try:
            # An empty prompt with a zero lease is Ollama's own way of saying
            # "unload this now" - it does no work and returns immediately.
            httpx.post(f"{base}/api/generate",
                       json={"model": name, "prompt": "", "keep_alive": 0},
                       timeout=timeout)
            let_go.append(name)
        except httpx.HTTPError:
            pass
    return let_go


# Which API a given address speaks, remembered after the first successful call
# so the fallback is paid for once rather than on every question.
_style: dict[str, str] = {}


def api_style(url: str) -> str:
    """"ollama" or "openai", whichever that address answers to."""
    return _style.get(url.rstrip("/"), "")


def _ask(url: str, model: str, prompt: str, timeout: float,
         threads: int, keep_alive: str) -> str:
    """One question, one answer, whichever API the server speaks."""
    base = url.rstrip("/")
    known = _style.get(base)

    if known != "openai":
        try:
            raw = _ask_ollama(base, model, prompt, timeout, threads, keep_alive)
            _style[base] = "ollama"
            return raw
        except httpx.HTTPStatusError as exc:
            # 404/405 means no native endpoint - an OpenAI-compatible server.
            # Anything else is a real failure and must not be retried as a
            # different protocol, or a busy Ollama looks like a missing one.
            if exc.response.status_code not in (404, 405) or known == "ollama":
                raise

    raw = _ask_openai(base, model, prompt, timeout)
    _style[base] = "openai"
    return raw


def _ask_ollama(base: str, model: str, prompt: str, timeout: float,
                threads: int, keep_alive: str) -> str:
    resp = httpx.post(
        base + "/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": True,
            # Hand the VRAM back after the last question, so a film starting
            # on Plex has the card to transcode with.
            "keep_alive": keep_alive,
            # Thinking and the answer share num_predict; too small and the
            # answer comes back empty after a minute of work. num_gpu forces
            # every layer onto the GPU - left to itself Ollama will quietly put
            # half the model on the CPU and peg eight cores for four minutes.
            # num_thread caps what is left: measured 5.5 cores at default,
            # 1.7 at two threads, for 25s versus 37s of work.
            "options": {"temperature": 0, "num_ctx": 16384,
                        "num_predict": 16384, "num_gpu": 99,
                        **({"num_thread": threads} if threads else {})},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return (resp.json() or {}).get("response", "")


def _ask_openai(base: str, model: str, prompt: str, timeout: float) -> str:
    """Chat completions, for a server that already hosts your Whisper.

    None of Ollama's knobs exist here - no keep_alive, no thread cap - because
    the API has nowhere to put them. On a shared GPU that is a real difference,
    which is why the native path is still preferred when it answers.
    """
    endpoint = base if base.endswith("/chat/completions") else (
        base + ("/chat/completions" if base.endswith("/v1")
                else "/v1/chat/completions"))
    resp = httpx.post(
        endpoint,
        json={"model": model, "temperature": 0,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json() or {}
    choices = body.get("choices") or [{}]
    message = choices[0].get("message") or {}
    # Some servers put the chain of thought in its own field and leave content
    # empty; the verdict is parsed out of either.
    return str(message.get("content") or message.get("reasoning_content") or "")


def adjudicate(items: list[dict], url: str, model: str,
               timeout: float = TIMEOUT, max_questions: int = 12,
               threads: int = 2, keep_alive: str = "30s",
               progress=None) -> dict[int, tuple[str, str]]:
    """{n: (verdict, instead)} for the entries Ollama could judge.

    One request per entry, with thinking on. Both parts are load-bearing and
    were measured, not assumed: with thinking off this model cleared real
    profanity and kept a scene's worth of "caulk" muted, and batching several
    entries into one request made it think past its token budget and return an
    empty answer. One at a time with room to think was right on every real
    case tried.

    Anything not answered is simply absent, and the caller keeps its own
    decision - which is to mute. `max_questions` bounds how long a file with
    unusually many ambiguous words can spend here.
    """
    if not url or not items:
        return {}

    from . import db      # local import: judge.py is used standalone in tests

    out: dict[int, tuple[str, str]] = {}
    # A question already answered is not asked again. The same word in the same
    # sentence has the same answer forever, and each one costs a model 30-100
    # seconds of thinking - so re-cleaning a file after a word-list change, or
    # repairing one, should not sit through all of them a second time.
    pending = []
    for item in items:
        key = _cache_key(item["line"], model)
        hit = db.judge_lookup(key)
        if hit:
            out[item["n"]] = hit
        else:
            pending.append(item)

    asked = pending[:max_questions]
    for position, item in enumerate(asked):
        last = position == len(asked) - 1
        # Say which question is being asked before it is asked. Each one takes
        # 30-100 seconds of a model thinking, and a stage that sits still for
        # five minutes is indistinguishable from a stuck one.
        if progress:
            progress(position, len(asked), _word_of(item["line"]))
        try:
            raw = _ask(url, model, PROMPT + f'{item["n"]}. {item["line"]}',
                       timeout=timeout, threads=threads,
                       keep_alive="0s" if last else keep_alive)
        except (httpx.HTTPError, ValueError, KeyError):
            continue
        answers = _parse(raw)
        out.update(answers)
        for n, (verdict, instead) in answers.items():
            if n == item["n"]:
                db.judge_remember(_cache_key(item["line"], model), verdict, instead,
                                  _word_of(item["line"]))
    return out


def _word_of(line: str) -> str:
    """The word a question is about, for the progress message."""
    match = re.search(r'the word "([^"]+)"', line or "")
    return match.group(1) if match else "a word"


def _cache_key(line: str, model: str) -> str:
    """One question: this word, in this sentence, judged by this model."""
    return hashlib.sha256(f"{model}\n{line}".encode("utf8")).hexdigest()


def _parse(raw: str) -> dict[int, tuple[str, str]]:
    text = re.sub(r"<think>.*?</think>", "", str(raw or ""), flags=re.S)
    text = text.replace("```json", "").replace("```", "").strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out: dict[int, tuple[str, str]] = {}
    for entry in parsed if isinstance(parsed, list) else []:
        try:
            n = int(entry["n"])
            verdict = str(entry["verdict"]).strip().upper()
        except (KeyError, TypeError, ValueError):
            continue
        instead = str(entry.get("instead", "") or "").strip()
        # A clear has to name what was really said. Without that it is a bare
        # assertion that the word was fine, which is the one answer that lets
        # profanity through - so it is treated as no answer at all.
        if verdict == "CLEAN" and not instead:
            verdict = "PROFANE"
        if verdict in ("PROFANE", "CLEAN"):
            out[n] = (verdict, instead)
    return out
