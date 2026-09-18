# Cleanarr

Cleanarr is an *arr-inspired application for anyone who wants to mute profanity
from their media — a way to carry on enjoying your content without the foul
language.

It adds a second audio track, **“Cleaned - English”**, to shows and movies you
already own, with the profanity muted. The original track is untouched and
stays the default; in Plex you pick the clean one from the audio menu of the
same episode. One file, two tracks — not two copies.

Built for a household with a small child in the room, so the bias throughout is
towards silence: when anything is uncertain, the word gets muted.

```
 Sonarr / Radarr ──▶ queue ──▶ ffmpeg ──▶ faster-whisper ──▶ word list
                                                                  │
                                    file with a new track ◀── mute + remux
```

## How it works

1. **Probe.** ffprobe reads the file. A file that already has a cleaned track
   is skipped unless you ask for it again.
2. **Listen.** The chosen English track is transcribed by faster-whisper on the
   GPU, with word-level timestamps — line-level subtitle timing would mean
   muting four seconds of dialogue to catch one word. Transcripts are cached
   against the audio, so re-cleaning a file after changing the word list skips
   this step entirely.
3. **Match.** Whole-word matching against the categories you switched on, with
   compound forms listed rather than guessed at. See `server/cleanarr/words.py`.
4. **Second opinion.** Only for words that are ambiguous in context — "cock",
   "ass", "hell", "Christ" — the sentence around them goes to Ollama, which
   says whether it was profanity. See below; this is not optional decoration,
   it is what stops a scene about caulking from being censored nine times.
5. **Mute.** ffmpeg's volume filter silences each span with a 20ms fade at each
   edge. The track is never cut, so it cannot drift out of sync.
6. **Remux and verify.** The new track is added with `-c copy`, then ffprobe
   checks the result has every stream the original had, plus one, at the same
   duration. Only then does it replace the original, keeping owner, permissions
   and modification time.

## The second opinion, and why it is shaped this way

Whisper writes homophones. An episode of *The Bear* set around drywall produced
nine mutes of "cock" — the word was "caulk". The word list cannot fix that: the
word really is on the list and the sentence really does contain it.

So ambiguous words, and only those, are sent to Ollama with their context.
Three findings from testing against that episode, each of which changed the
design:

| Tried | Result |
|---|---|
| One request for all ambiguous words, thinking off | Cleared real profanity, kept "caulk" muted. Unusable. |
| One request for all of them, thinking on | Thought past its token budget, returned an empty answer. |
| **One request each, thinking on, 16k budget** | **Right on every real case: caulk cleared, "ass", "crap", "Christ", "oh God" and "dickheads" all kept muted.** |

Repeated occurrences of one word in a scene are pooled into a single question,
which is both cheaper and more consistent — the drywall scene only reads as
caulk when you see several quotes together.

Two rules keep it safe: a clear is only accepted if the model names the
ordinary word it heard instead, and anything unanswered — Ollama down, garbled
JSON, timeout — stays muted.

Every decision here was made against a real library and most of them reversed
something that seemed obvious. A few worth knowing before you change them:

* **medium.en is the only model offered.** large-v3 took 50% longer and caught
  *fewer* swears - it favours tidy prose, and tidy prose smooths swearing away.
* **Whisper's silence filter is off.** On one 43-minute episode it skipped 19
  real profanities and saved no measurable time.
* **The second opinion is off by default.** Over 196 checks it changed the
  outcome twice, at 30-100 seconds each.

## Installing it on a phone

Cleanarr is a progressive web app: its own icon, its own window, no browser
bar. Open it on the phone and use **Settings -> Install it on a phone**, or the
browser's own *Install app* menu item.

> **Browsers only offer a real install over HTTPS.** On a plain `http://` LAN
> address, Chrome and Brave give you a bookmark with a browser bar instead of
> an app. Put Cleanarr behind a reverse proxy with a certificate - Nginx Proxy
> Manager, Caddy, Traefik - and the install offer appears. Worth doing anyway
> if you have set a password.

## Running it

```bash
cd docker && docker compose up -d --build
```

Then open `http://<host>:8477` and fill in Settings. It needs:

* An NVIDIA GPU with the container toolkit (an RTX 2070 Super does a 31-minute
  episode in about a minute of listening).
* Sonarr and Radarr addresses and API keys, read-only.
* The media mounted at the same path Sonarr and Radarr use — the compose file
  mounts `/mnt/user/data:/data` for exactly that reason. If your paths differ,
  set `path_map` in `/config/config.yaml`.
* Ollama, for the second opinion. Leave the address blank to turn it off, and
  every ambiguous word is muted instead.

### On Unraid

`docker/my-Cleanarr.xml` goes on the flash at
`/boot/config/plugins/dockerMan/templates-user/`. With it in place the Docker
tab gives the container its icon, a **WebUI** link (no port to remember) and a
working **Edit** dialog for ports, paths and variables.

Two things follow from that, and neither is a fault:

* The template and `docker-compose.yml` describe the same container, and either
  can recreate it. Change a port or a path in one, change it in the other.
* The image is built here rather than pulled, so Unraid's "check for updates"
  has no registry to ask. Rebuild with `docker compose up -d --build`.

The compose file also carries `net.unraid.docker.webui` and `.icon` labels, so
the link and icon survive even when compose is what created the container.

## What it costs

Measured end to end on a 25-minute episode with nothing cached, on a Ryzen
2700X with an RTX 2070 Super:

| Stage | Time | CPU | GPU |
|---|---|---|---|
| Listening (medium.en) | ~85s | ~1 core | 75% |
| Second opinion | ~35s per ambiguous group | ~1.6 cores | 85% |
| Mute and remux | ~35s | ~1 core | — |
| **Total** | **~2.5 min** | never above ~2 cores of 16 | |

File growth: +90 MB for a 25-minute episode (E-AC-3 384k, matching the
original's codec so Plex direct plays it).

## Staying out of the way

The first version made the server unusable — a 31-minute episode took 337
seconds and pinned **eight cores for four straight minutes**, while Plex became
unwatchable. The profile showed the GPU at 10% throughout, which gave the cause
away: Whisper was holding 2GB of the 8GB card, so Ollama could only fit part of
the model and ran the rest on the CPU.

Five changes, all measured on the same episode:

| Change | Effect |
|---|---|
| Unload Whisper before the second opinion | Ollama fits on the GPU: 230s → 25s for the same question, and no CPU inference |
| `num_gpu: 99` on the request | Stops Ollama quietly splitting the model again |
| `num_thread: 2` | 5.5 cores → 1.7, costing 25s → 37s per question |
| `keep_alive: 0` after the last question | Hands ~7GB of VRAM straight back, so Plex can transcode |
| ffmpeg `-threads 2`, `cpu_shares: 512` | The remux cannot crowd anything out |

Result on that episode: 337s → 160s, with CPU never above ~2 cores. Plex's API
answers in about 1ms during a job, the same as when idle.

### Waiting for Plex, but only when it matters

Transcribing and judging both want the GPU Plex transcodes on, and no amount of
thread limiting changes that. But **a direct play costs this machine nothing**,
so "is anyone watching?" is the wrong question - the first version asked it and
sat idle while someone watched a file Plex was handing over untouched.

What it asks now is what Plex is actually *doing*, from `/status/sessions`:

| Policy | Waits for |
|---|---|
| `never` | nothing |
| `video_transcode` (default) | only a session whose **video** is being transcoded |
| `any_transcode` | video or audio transcoding |
| `playing` | anything at all, including direct play |

The Queue page names what it is waiting for ("Rylee is watching X (transcoding
video on the GPU)") and offers **Clean anyway**, which ignores the hold until
the queue runs dry and then goes back to normal.

## Shows that clean themselves

Cleaning a season, or an episode of a show, marks that show. Every ten minutes
the monitored shows are compared against the job history, and anything Sonarr
has that has not been cleaned is queued - which covers both the episodes that
were already sitting there and the ones that have not aired yet.

**"Clean newly downloaded episodes" means exactly that** and nothing else:
episodes Sonarr imports from now on, judged by the file's import date. Nothing
already on disk is touched. Cleaning a season also ticks it, because wanting
this season clean implies wanting next week's episode clean too.

Cleaning what is already there is a separate button - **Clean everything not
cleaned yet** - which says how many episodes and roughly how long before it
queues anything.

That separation was learned the hard way. The toggle originally meant "and
catch up on the back catalogue", so ticking it on Grey's Anatomy queued 465
episodes - about 19 hours - when what was wanted was next week's episode. One
checkbox, one meaning.

Checking happens **every ten minutes on its own thread**, not in the worker
loop - the worker spends most of its life inside a single job, so sharing the
thread meant an episode that landed during a long queue waited for the whole
queue before anyone noticed it. The Queue page says when it last looked.

For cleaning to start the moment a download finishes instead, point a Sonarr
**Connect -> Webhook** with the *On Import* trigger at
`http://<host>:8477/api/webhook/sonarr`. It answers immediately and does the
work behind the reply, because Sonarr retries a slow webhook.

The Queue page also has **Check for new episodes** for impatience, and **Empty
the queue** for when something was queued that should not have been.

## The order things run in

The queue runs in the order you asked for it - first asked, first cleaned - and
anything added later goes to the back. Two exceptions:

* **A newly downloaded episode jumps to the front.** It is the one someone is
  waiting to watch tonight; a back catalogue can wait another hour.
* **You can move any waiting job** with the ⤒ ▲ ▼ ⤓ controls on its row.

Cancelled jobs are dropped from the Queue view entirely - cancelling a season
leaves dozens of rows that say only "you changed your mind" - and **Remove
cancelled** deletes them for good.

### Why not run it on the other machine?

Worth recording, because it looks like the obvious answer: the judge was tested
against the Bazzite box, which has a 16GB AMD card. It is **slower**, not
faster — 185s for the 9b model and 95s for the 35b MoE, against 25-49s on
Tower's 2070 Super. Dense models do poorly on that card. Offloading there would
trade a fast local answer for a slow remote one; the real fix was giving the
local GPU room to work.

## On a phone

The page is built for a phone as much as a desktop: a manifest and icons for
adding it to a home screen, safe-area padding so nothing hides under a notch or
home indicator, thumb-sized targets, a tab strip that scrolls, three posters to
a row, and the show sheet filling the screen.

Static files are served `no-cache` (artwork excepted) so an update never leaves
a browser holding yesterday's stylesheet - which looks exactly like a broken
layout and is miserable to diagnose from a phone.

Chrome will only *install* a page served over HTTPS, so on plain
`http://<host>:8477` Android offers a home-screen shortcut rather than a true
app window. Behind the reverse proxy with a certificate it installs properly;
iOS's Add to Home Screen honours the manifest either way.

## The page

Four tabs. **Shows** and **Movies** are a poster wall like Sonarr's, with
search, filters, and badges for what is cleaned, queued or auto-cleaning.
Opening a show lists its seasons collapsed - a 37-season show is a short list
until you open one - with *Clean the season*, *Clean the whole show*, and the
auto-clean toggle.

**Queue** shows what is running, what is waiting, and why it might be holding.
**Cleaned** is the searchable record of what has been done: when, how many
words, and the full list of what was muted or deliberately left in.

Anything already cleaned says so before it is cleaned again - one episode names
the date and the count, a season says how many of its episodes were already
done and offers to skip them.

## Layout

```
server/cleanarr/
  words.py      the word lists and whole-word matching   (tools/test_words.py)
  judge.py      the second opinion on ambiguous words     (tools/test_judge.py)
  asr.py        faster-whisper, with transcript caching
  media.py      ffprobe/ffmpeg: probe, mute, remux, verify, swap
  pipeline.py   one file, start to finish
  worker.py     the queue - one job at a time, on purpose
  arr.py        Sonarr and Radarr, read-only
  main.py       API and static page
web/            the page
```

Tests are plain scripts, no framework: `python tools/test_words.py`,
`python tools/test_judge.py`.

## Things worth knowing

* **One job at a time.** The GPU is shared with Plex transcodes and Ollama.
* **The model is unloaded after ten idle minutes**, giving its VRAM back.
* **Nothing is written to Sonarr or Radarr.** They are only asked what exists.
* **Re-cleaning** replaces the previous cleaned track rather than adding a
  second one. Use it after changing the word lists — the transcript is cached,
  so it is quick.
* **"Never silence"** in Settings is where a name that keeps getting caught
  goes. "Always silence" is the opposite.
