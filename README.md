# Cleanarr

Cleanarr is an \*arr-inspired, *vibe-coded* application for anyone who wants
to mute profanity from their media — a way to carry on enjoying your content
without the foul language.

It adds a second audio track, **“Cleaned - English”**, to shows and movies you
already own, with the profanity muted. The original track is untouched and
stays the default; in Plex or Jellyfin you pick the clean one from the audio
menu of the same episode. **One file, two tracks — not two copies.**

Built for a household with a small child in the room, so the bias throughout is
towards silence: when anything is uncertain, the word gets muted.

```
 Sonarr / Radarr ──▶ queue ──▶ ffmpeg ──▶ Whisper ──▶ word list
                                                          │
                            file with a new track ◀── mute + remux
```

---

## What it looks like

**Home** — what has landed lately, and what it has cost so far.

![Home](docs/images/home.jpg)

**Your library**, laid out the way Sonarr and Radarr do it. Badges show what is
cleaned, queued, auto-cleaning, or has nothing on disk yet.

![Shows](docs/images/shows.jpg)

**Inside a show**, seasons collapse like Sonarr's. Clean one episode, a season,
everything not done yet, or *from here on* — and take a cleaned track back out
just as easily.

![Episodes](docs/images/episodes.jpg)

**Every mute is on the record.** Each cleaned file lists what was silenced and
when, so a wrong call can be found rather than guessed at — and anything the
second opinion left in is shown too, with its reason.

![Job details](docs/images/details.jpg)

**Cleaned** is the searchable history: when, how many words, and what the extra
tracks are costing in disk.

![Cleaned](docs/images/cleaned.jpg)

**Upcoming** is Sonarr's calendar without leaving the app — what airs when,
what has already downloaded and how long after airing it arrived. Tick
*Clean it* and that show cleans itself from then on.

![Upcoming](docs/images/upcoming.jpg)

---

## Requirements

| | |
|---|---|
| **GPU** | Optional. An NVIDIA GPU is much faster, but CPU works, and an AMD card can be used through a separate Whisper server. See [Hardware](#hardware). |
| **A library** | Sonarr and Radarr, **or** Plex, **or** Jellyfin. Read-only either way — Cleanarr never writes to any of them. |
| **Media** | Mounted at the **same paths** Sonarr and Radarr report. See [Paths](#paths). |
| **Disk** | ~1.5 GB for the speech model, plus roughly 90 MB per cleaned 25-minute episode. |
| **Ollama** | Optional, off by default. See [The second opinion](#the-second-opinion). |

---

## Installing

### Docker Compose

```yaml
services:
  cleanarr:
    image: ghcr.io/geoguy89/cleanarr:latest
    container_name: cleanarr
    restart: unless-stopped
    stop_grace_period: 45s
    ports:
      - "8477:8477"
    volumes:
      - /path/to/appdata/cleanarr:/config
      # Cleanarr opens files at the paths your library reports. Mount your
      # media so those paths work here too - and when you can't, Settings has
      # a path translator that checks itself.
      - /path/to/media:/data
    environment:
      - TZ=America/New_York
```

```bash
docker compose up -d
```

> Also on Docker Hub as `geoguy89/cleanarr:latest` if you prefer it. GHCR is
> the better default: Docker Hub rate-limits anonymous pulls to 100 per six
> hours per IP, and an install that fails on that gives a confusing error.

### docker run

```bash
docker run -d \
  --name cleanarr \
  --restart unless-stopped \
  -e TZ=America/New_York \
  -p 8477:8477 \
  -v /path/to/appdata/cleanarr:/config \
  -v /path/to/media:/data \
  ghcr.io/geoguy89/cleanarr:latest
```

**With an NVIDIA GPU** add the lines below as well - transcription is several
times faster. Add them **only** if you have one: Docker refuses to start a
container whose device reservation cannot be met, so on a machine without an
NVIDIA card, or without the container toolkit installed, these stop Cleanarr
running at all and the error says nothing about Cleanarr. Leave them out and
everything works, with Whisper transcribing on the CPU.

```yaml
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=compute,utility
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

For `docker run` the equivalent is `--runtime nvidia -e
NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=compute,utility`.

### Unraid

Copy `docker/my-Cleanarr.xml` to the flash drive at
`/boot/config/plugins/dockerMan/templates-user/`, then **Docker → Add
Container** and choose *cleanarr* from the template dropdown.

The template gives the container its icon, a **WebUI** link so there is no port
to remember, and a working **Edit** dialog for ports, paths and variables.

Set **Media** to the same host path Sonarr and Radarr use, and leave its
container path as `/data` unless theirs differs.

> **Updates work normally.** The template pulls from ghcr.io, so Unraid's
> *check for updates* asks the registry and offers the new image in the Docker
> tab, the same as any other container.

### Building it yourself

The published image is `linux/amd64` only, because its CUDA base has no arm64
build. Building locally also gets you whatever is on `main` rather than the
last release.

```bash
git clone https://github.com/geoguy89/cleanarr.git
cd cleanarr/docker && docker compose up -d --build
```

That compose file carries its own `build:` section, so it compiles the image
instead of pulling one. Use it if `docker compose up -d` on the example above
fails with **pull access denied** — that means the registry does not have the
image yet, not that you lack permission.

### Hardware

The listening is the only demanding part, and there are three ways to do it.

| | How | Speed |
|---|---|---|
| **NVIDIA GPU** | Add the runtime and device lines above | ~85 s for a 25-minute episode |
| **CPU only** | Drop those lines; it falls back on its own | Several times slower — usually still faster than watching the episode |
| **AMD GPU** | The built-in Whisper still installs and runs — on the CPU, not the card. To use the card, point Cleanarr at a Whisper server that supports it | CPU speed, or the server's |

**On AMD:** you can still install normally and download the model — nothing
is blocked. It simply will not use your card. faster-whisper runs on
CTranslate2, which has CPU and CUDA backends and no ROCm, so the GPU sits idle
and the work lands on the CPU.

To actually use an AMD card, run something that supports it and point Cleanarr
at that: [whisper.cpp](https://github.com/ggml-org/whisper.cpp) has Vulkan and
ROCm backends, and anything exposing the OpenAI transcription API works here.

**CPU only** needs no configuration - just leave the GPU lines out. Cleanarr
detects there is no CUDA device and uses the CPU. Expect minutes rather than seconds per episode,
and consider queueing overnight rather than cleaning something you are about to
watch.

**To use a separate Whisper server**, install without the `nvidia` runtime and
device lines, then under **Settings → Listening** choose **Use my own Whisper
server** and give it the address of a
[speaches](https://github.com/speaches-ai/speaches), `faster-whisper-server`,
whisper.cpp or LocalAI instance.

> That server must return **word-level** timestamps. Cleanarr mutes half a
> second around one word; with only line-level timings the best it could do is
> blank four seconds of dialogue to catch one syllable. There is a **Test**
> button that checks for this specifically.

---

## First run

Open `http://<host>:8477` and work down **Settings** — it is numbered in the
order things need doing.

1. **Your library** — Sonarr and Radarr, with a *Test* button for each.
2. **What gets silenced** — four switchable lists, plus your own always/never
   words.
3. **Listening** — download the speech model here, rather than letting it
   happen silently during your first clean.
4. **Muting** — padding and fade. The defaults are fine.
5. **Second opinion** — optional, off by default.
6. **Media server** — Plex or Jellyfin, also optional.
7. **Who can use this** — set a password if this is reachable from anywhere you
   do not control.

Then clean a single episode before turning it loose on a season.

### Where the library comes from

Cleanarr needs one thing: a list of what you own and where each file is.
Three things can answer that, chosen in **Settings → Your library**.

| Source | Needs | Notes |
|---|---|---|
| **Sonarr + Radarr** (default) | both, with API keys | Knows the most. The **only** one that can fill the Upcoming calendar, because it is the only one that knows what has not aired yet. |
| **Plex** | address + token | No \*arr apps needed. Artwork comes from Plex too. |
| **Jellyfin** | address + API key | Same again. |

Everything else works identically whichever you pick, including cleaning new
episodes automatically — both media servers record when an item was added,
which is the same signal Sonarr's import history gives. The **Upcoming** tab
hides itself on Plex and Jellyfin rather than showing an empty page.

The Plex and Jellyfin credentials are the same ones used for refreshing the
library and pausing while it transcodes; there is no second set to keep in step.

### Paths

There is no media folder to configure. **Sonarr and Radarr say where every file
is, per file, and Cleanarr opens exactly that path** — TV paths from Sonarr,
film paths from Radarr.

So this container has to see your media at the *same* paths those two report.
If Sonarr says `/data/media/tv/…` and Radarr says `/data/media/movies/…`, mount
`/data` here exactly as they have it.

A job that fails with *“… not found from this container”* is almost always
this. If your mounts genuinely cannot match, `path_map` in
`/config/config.yaml` rewrites the start of a path:

```yaml
path_map:
  "/media": "/data"
```

It is deliberately not on the settings page — it is a workaround for a mismatch
better fixed properly.

---

## How it works

1. **Probe.** ffprobe reads the file. One already carrying a cleaned track is
   skipped unless you ask again.
2. **Extract.** ffmpeg pulls the English audio out as 16 kHz mono.
3. **Listen.** Whisper transcribes it with **word-level timestamps**. This is
   the whole point: a subtitle line says a swear happened somewhere in four
   seconds, words give ~50 ms.
4. **Match.** Whole-word matching, so *class* and *cockpit* are never caught by
   what is inside them. A phrase mutes only the profane word in it — *“oh my
   god”* silences *god* and leaves *oh my* audible.
5. **Mute.** Each match is silenced with a 20 ms fade at each edge.
6. **Verify, then swap.** The new file replaces the original **only** after
   ffprobe confirms it has every stream the original had plus one, at the same
   duration, correctly interleaved.

Transcripts are cached against the audio, so changing a word list and
re-cleaning skips the slow part entirely.

**What it costs** — a 25-minute episode, nothing cached, Ryzen 2700X + RTX 2070
Super: about **85 s** listening, **35 s** muting and remuxing, **~2.5 minutes**
total, never above ~2 cores of 16. The file grows by roughly **90 MB**, and the
cleaned track matches the original's codec so it still direct plays.

---

## The second opinion

**Optional, off by default, and most people should leave it off.**

Speech recognition writes homophones. A scene in *The Bear* about drywall
produced **nine mutes of “cock”** — the word was **“caulk”**. No word list can
fix that, because the transcript genuinely contains the rude one.

So a word you list is sent to **your own Ollama**, on your own hardware, with
the sentence around it, and is left in only if the model can name the ordinary
word it heard instead. Nothing leaves your network, and a blank address skips
the step entirely.

Why it defaults to off: across **196 checks** on a real library it changed the
outcome **twice**, at 30–100 seconds each.

Two rules keep it safe — a clear is accepted only if the model names the
ordinary word, and anything unanswered (Ollama down, garbled JSON, timeout)
stays muted.

---

## Shows that clean themselves

Marking a show means **episodes downloaded from now on** are cleaned as they
arrive. Nothing already on disk is touched; catching up the back catalogue is a
separate, deliberate button.

Checked every 10 minutes. For instant cleaning, point a Sonarr **Connect →
Webhook** (*On Import*) at `http://<host>:8477/api/webhook/sonarr`.

A show with no files yet can be marked too, so a new series arrives clean
without a second visit.

---

## Things worth knowing

All measured, and most of them reversed something that seemed obvious.

* **medium.en is the only model offered.** large-v3 took 50% longer and caught
  *fewer* swears — it favours tidy prose, and tidy prose smooths swearing away.
  small.en misses noticeably more.
* **Whisper's silence filter is off.** On one 43-minute episode it skipped 19
  real profanities and saved no measurable time.
* **Nothing is written until it verifies.** An early version produced a file
  Plex hung on, because the cleaned audio ended up 485 MB away from the picture
  inside the container. The interleave check exists because of that episode and
  refuses to install a file that fails it.
* **It waits for your media server only when that matters.** A direct play
  costs nothing, so by default only a *video* transcode holds the queue.
* **One job at a time**, on purpose — the GPU is shared.
* **Nothing is ever written to Sonarr or Radarr.** They are only asked what
  exists.

---

## Development

```
server/cleanarr/
  words.py      word lists and whole-word matching     (tools/test_words.py)
  judge.py      the second opinion                     (tools/test_judge.py)
  asr.py        Whisper, local or remote, with caching
  media.py      ffprobe/ffmpeg: probe, mute, remux, verify, swap
  pipeline.py   one file, start to finish
  worker.py     the queue
  arr.py        Sonarr, Radarr, Plex, Jellyfin
  auth.py       optional login
  main.py       API and static page
web/            the page
```

Tests are plain scripts, no framework:

```bash
python tools/test_words.py
python tools/test_judge.py
```

---

## Licence

MIT — see [LICENSE](LICENSE).
