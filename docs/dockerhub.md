<!--
  This is the text for the Docker Hub page, kept here so it lives with the
  project rather than only in a web form.

  It is NOT published automatically. Docker Hub's description API wants a JWT
  from a username and PASSWORD; a personal access token is refused, and putting
  an account password in a CI secret to save one paste is a poor trade - the
  token is scoped and revocable, the password is not.

  So when this changes, paste it into:
  https://hub.docker.com/repository/docker/geoguy89/cleanarr/general
-->

# Cleanarr

Cleanarr is an \*arr-inspired, *vibe-coded* application for anyone who wants
to mute profanity from their media — a way to carry on enjoying your content
without the foul language.

It adds a second audio track — “Cleaned - English”, or whatever you name it — to shows and movies you
already own, with the profanity muted. The original track is untouched and
stays the default; in Plex or Jellyfin you pick the clean one from the audio
menu of the same episode. **One file, two tracks — not two copies.**

📖 **[Full documentation, installation and screenshots on GitHub](https://github.com/geoguy89/cleanarr)**

![Cleanarr](https://raw.githubusercontent.com/geoguy89/cleanarr/main/docs/images/home.jpg)

---

## Quick start

```yaml
services:
  cleanarr:
    image: geoguy89/cleanarr:latest
    container_name: cleanarr
    restart: unless-stopped
    stop_grace_period: 45s
    ports:
      - "8477:8477"
    volumes:
      - /path/to/appdata/cleanarr:/config
      # The right-hand side must be the path your library reports;
      # Cleanarr opens those paths unchanged.
      - /path/to/media:/data
    environment:
      - TZ=America/New_York
    # With an NVIDIA GPU, uncomment these. Without one they stop the
    # container starting, so leave them out and it runs on the CPU.
    # environment:
    #   - NVIDIA_VISIBLE_DEVICES=all
    #   - NVIDIA_DRIVER_CAPABILITIES=compute,utility
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - driver: nvidia
    #           count: all
    #           capabilities: [gpu]
```

### Or with docker run

```bash
docker run -d \
  --name cleanarr \
  --restart unless-stopped \
  -e TZ=America/New_York \
  -p 8477:8477 \
  -v /path/to/appdata/cleanarr:/config \
  -v /path/to/media:/data \
  geoguy89/cleanarr:latest
```

With an NVIDIA GPU, add `--runtime nvidia -e NVIDIA_VISIBLE_DEVICES=all -e
NVIDIA_DRIVER_CAPABILITIES=compute,utility`. Then open `http://<host>:8477`:
Home lists what is left to set up.

> **Also on GitHub Container Registry** as `ghcr.io/geoguy89/cleanarr:latest`,
> if you would rather pull from there. Docker Hub rate-limits anonymous pulls
> to 100 per six hours per IP; GHCR does not.

---

## What it does

1. **Probe** — ffprobe reads the file; one already carrying a cleaned track is skipped.
2. **Extract** — ffmpeg pulls the English audio out as 16 kHz mono.
3. **Listen** — Whisper transcribes it with **word-level timestamps**. A subtitle line says a swear happened somewhere in four seconds; words give ~50 ms.
4. **Match** — whole-word matching, so *class* and *cockpit* are never caught by what is inside them. A phrase mutes only the profane word: *“oh my god”* silences *god* and leaves *oh my* audible.
5. **Mute** — each match silenced with a 20 ms fade at each edge.
6. **Verify, then swap** — the new file replaces the original **only** after ffprobe confirms it has every stream the original had plus one, at the same duration, correctly interleaved.

About **2.5 minutes** for a 25-minute episode on an RTX 2070 Super, never above ~2 cores of 16. The file grows by roughly 90 MB.

---

## Requirements

| | |
|---|---|
| **GPU** | Optional. NVIDIA is much faster; CPU works with no configuration; an AMD card needs a separate Whisper server. |
| **A library** | Sonarr + Radarr, **or** Plex, **or** Jellyfin. Read-only — Cleanarr never writes to any of them. |
| **Media** | Mounted so the paths your library reports exist in the container. There is no path-rewriting setting. |
| **Ollama** | Optional, off by default. Any OpenAI-compatible chat server works too. |

**Platform:** `linux/amd64` only — the CUDA base has no arm64 build.

---

## Also worth knowing

- **Shows can clean themselves** — mark one and episodes downloaded from then on are cleaned as they arrive.
- **The cleaned track lives inside the media file**, as an extra audio stream, not as a copy of the episode. The file is built in a scratch folder beside the original and swapped in only after it verifies; a failed job leaves the file exactly as it was. Settings, history, the speech model and transcripts live in `/config`.
- **Nothing is ever written to your library.** It is only asked what exists.
- **A wrong call is one click to fix.** Each job lists every word it muted; "That was wrong" puts the word on a list for next time.
- **Optional login**, off until you set one.

MIT licensed. Issues and pull requests: <https://github.com/geoguy89/cleanarr>
