# Cleanarr

Cleanarr is an \*arr-inspired, *vibe-coded* application for anyone who wants
to mute profanity from their media — a way to carry on enjoying your content
without the foul language.

It adds a second audio track, **“Cleaned - English”**, to shows and movies you
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
    image: tswillette/cleanarr:latest
    container_name: cleanarr
    restart: unless-stopped
    stop_grace_period: 45s
    ports:
      - "8477:8477"
    volumes:
      - /path/to/appdata/cleanarr:/config
      # Must match how Sonarr and Radarr see your media.
      - /path/to/media:/data
    environment:
      - TZ=America/New_York
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

Then open `http://<host>:8477` and work down Settings.

> **Also on GitHub Container Registry:** `ghcr.io/geoguy89/cleanarr:latest`.
> Worth preferring — Docker Hub rate-limits anonymous pulls to 100 per six
> hours per IP, and GHCR does not.

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
| **Sonarr / Radarr** | Addresses and API keys. Read-only — Cleanarr never writes to either. |
| **Media** | Mounted at the **same paths** Sonarr and Radarr report. |
| **Ollama** | Optional, off by default. |

**Platform:** `linux/amd64` only — the CUDA base has no arm64 build.

---

## Also worth knowing

- **Shows can clean themselves** — mark one and episodes downloaded from then on are cleaned as they arrive.
- **Nothing is written until it verifies.** A failed job leaves the file exactly as it was.
- **Nothing is ever written to Sonarr or Radarr.** They are only asked what exists.
- **Optional login**, off until you set one.

MIT licensed. Issues and pull requests: <https://github.com/geoguy89/cleanarr>
