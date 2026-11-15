---
trigger: always_on
description: Always-on 1-shot music playback commands and vague-vibe routing via ~/.local/bin/music.
---

## Music Playback (One-Shot `~/.local/bin/music`)

Run one single command immediately. Never probe `tracks.json`, never run `--help`, and never use `socat`, `ytdl://ytsearch:`, `mpv` directly, or `playerctl`.

| Intent | One-Shot Command |
| :--- | :--- |
| Specific song/artist (local first, seamless YouTube Music fallback) | `~/.local/bin/music play --search "<song or artist>" --online --json` |
| Local mood (`chill`, `focus`, `energetic`, `uplifting`, `late-night`, `nostalgic`, `romantic`, `melancholy`) | `~/.local/bin/music play --mood <mood> --json` |
| Local genre (`rock`, `alternative`, `pop`, `hip-hop`, `rnb-soul`, `electronic`, `jazz`, `funk-disco`, `folk-acoustic`, `blues`, `metal`, `soundtrack`, `world`, `ambient-chill`) | `~/.local/bin/music play --genre <genre> --json` |
| Local artist / playlist | `~/.local/bin/music play --artist "<name>" --json` / `--playlist "<name>" --json` |
| Known non-local, unique, obscure, remix, edit, or live track | `~/.local/bin/music online "<artist track or unique query>" --json` |
| Vague / exploratory online vibe (or queue of 3) | `~/.local/bin/music online "Artist A - Track 1" "Artist B - Track 2" "Artist C - Track 3" --json` (or `--count 3 "<concise style/artist query>" --json`) |
| Controls | `~/.local/bin/music pause\|resume\|toggle\|next\|previous\|stop\|status --json` |

### Vague-Vibe & Verification Rules
1. For vague vibes (e.g., "moody late-night Japanese jazz"), immediately translate the vibe using your own musical knowledge into 1–3 real, concrete artist/track queries for `~/.local/bin/music online`. Never use generic SEO spam words (like "best relaxing mix compilation playlist").
2. `music online` and `music play --online` already unpause and verify real `mpv` playback before returning `0`. Confirm what started playing from the JSON `display` or `title` field in one concise spoken sentence.
