"""Subtitle line-up audit.

For each file, the English text subtitles inside it are the answer key. They
are damaged the ways borrowed subtitles go wrong - a steady offset, drift at
advert breaks, another frame rate, another film's text - lined up again with
Cleanarr's own code, and compared with the key: where each line lands, and what
each detection is judged to be. "risky" counts detections the key does not call
"differs" but the damaged copy does: the verdict that can leave a word unmuted.

Nothing is written to the media. Run it in a throwaway container with the media
read-only and this install's config (for its settings and cached transcripts):

    docker run --rm --gpus all --entrypoint python3       -v /mnt/user/data:/data:ro -v /mnt/user/appdata/cleanarr:/config       -v "$PWD/tools:/tools" cleanarr:latest /tools/subtitle_audit.py files.txt out.json

files.txt lists one media path per line, as the container sees it.
"""
import json
import random
import statistics
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, "/app")
from cleanarr import asr, config, media, subtitles, words  # noqa

FILES = [l.strip() for l in open(sys.argv[1], encoding="utf8") if l.strip()]
OUT = sys.argv[2] if len(sys.argv) > 2 else "subtitle_audit.json"
CACHE = Path("/config/cache")
settings = config.load()


def heard_words(path: Path, work: Path):
    original = media.probe(path)
    source = media.pick_source_track(original)
    stream = media.pick_subtitles(original)
    subs = work / "subs.srt"
    wav = media.extract_for_asr(path, source, work / "asr.wav", subtitles=stream, subtitle_dest=subs)
    t = asr.transcribe(wav, model_name=settings.model, device=settings.device,
                       compute_type=settings.compute_type, trim_silence=settings.trim_silence,
                       cache_dir=CACHE, download_root=str(CACHE / "models"))
    offset = media.speech_offset(original, source)
    heard = [{**w, "start": float(w.get("start") or 0) + offset, "end": float(w.get("end") or 0) + offset}
             for w in t.words]
    cues = subtitles.parse_srt(subs.read_text(encoding="utf8", errors="replace")) if subs.exists() else []
    return heard, cues, t.cached, stream


def warp(cues, fn):
    return [subtitles.Cue(fn(c.start), fn(c.end), c.text) for c in cues]


def states(matches, cues, heard, matcher):
    return [subtitles.evidence(m, cues, matcher, heard)[0] for m in matches]


def in_line(cues, heard):
    """Share of heard words (4+ letters) that fall inside a line containing them."""
    cs = sorted(cues, key=lambda c: c.start)
    starts = [c.start for c in cs]
    vocab = [set(subtitles._WORD.findall(c.text.lower())) for c in cs]
    s = [(float(w["start"]), words.normalize(w["word"])) for w in heard]
    s = [(t, w) for t, w in s if len(w) >= 4]
    return round(subtitles._share(cs, starts, vocab, s, 0.0, 0.3), 3)


out = []
for f in FILES:
    path = Path(f)
    row = {"file": path.name}
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        try:
            heard, truth, cached, stream = heard_words(path, Path(tmp))
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"[:300]
            out.append(row)
            print(json.dumps(row), flush=True)
            continue
    row.update(words=len(heard), cues=len(truth), cached_transcript=cached,
               subtitle_track=getattr(stream, "title", "") or getattr(stream, "language", ""))
    if not truth:
        row["error"] = "no text subtitles"
        out.append(row)
        print(json.dumps(row), flush=True)
        continue
    matcher = words.Matcher(categories=tuple(settings.categories),
                            never=frozenset(words.NEVER) | {w.lower() for w in settings.allow_words},
                            extra=tuple(settings.custom_words),
                            context_words=frozenset(w.lower() for w in settings.check_in_context))
    matches = matcher.find(heard)
    ev = words.Matcher(context_words=frozenset())
    key, klo, khi = subtitles.align(truth, heard)
    key_states = states(matches, key, heard, ev)
    row["key"] = {"moved": [klo, khi], "in_line": in_line(key, heard),
                  "detections": len(matches),
                  "states": {s or "none": key_states.count(s) for s in set(key_states)}}
    end = max(c.end for c in truth)
    random.seed(7)
    texts = [c.text for c in truth]
    random.shuffle(texts)
    tests = {
        "offset +9s": warp(truth, lambda t: t + 9.0),
        "offset -45s": warp(truth, lambda t: t - 45.0),
        "ad-break drift 2/6/11s": warp(truth, lambda t: t + (2.0 if t < end / 3 else 6.0 if t < 2 * end / 3 else 11.0)),
        "PAL fast (x23.976/25) +3s": warp(truth, lambda t: t * 23.976 / 25 + 3.0),
        "PAL slow (x25/23.976) -2s": warp(truth, lambda t: t * 25 / 23.976 - 2.0),
        "wrong subtitles": [subtitles.Cue(c.start, c.end, x) for c, x in zip(truth, texts)],
    }
    row["tests"] = {}
    for name, bad in tests.items():
        fixed, lo, hi = subtitles.align(bad, heard)
        fits = subtitles.fits(fixed, heard)
        r = {"moved": [lo, hi], "fits": fits, "in_line": in_line(fixed, heard)}
        if name != "wrong subtitles":
            errs = sorted(abs(a.start - k.start) for a, k in zip(fixed, key))
            r["line_error_median"] = round(statistics.median(errs), 2)
            r["lines_within_0.5s"] = round(sum(e <= 0.5 for e in errs) / len(errs), 3)
        got = states(matches, fixed if fits else [], heard, ev)
        same = sum(a == b for a, b in zip(got, key_states))
        # The dangerous kind: the key says the subtitles have it (or leave it
        # out, or have no line), this says they say something else - which is
        # what leaves an unsure word unmuted.
        risky = [(round(m.start), m.text, k, g) for m, k, g in zip(matches, key_states, got)
                 if g == "differs" and k != "differs"]
        r.update(same_verdict=f"{same}/{len(matches)}", risky=risky[:6], risky_count=len(risky))
        row["tests"][name] = r
    out.append(row)
    print(json.dumps(row), flush=True)

json.dump(out, open(OUT, "w"), indent=1)
