# scraper `/stream` — audio-only extraction (2026-08-16)

`app/scraper/api/routes/stream.py` (the single source of truth; scraper-service
stages this subtree at deploy) now accepts **`?audio=true`**.

## What it proved

music-player reported "Audio playback error" on every YouTube track. The cause
was in THIS endpoint, not in the player: it only ever offered combined
progressive formats — `best[ext=mp4][height<=720]` → `best[ext=mp4]` → `best`,
all resolving to **itag 18** (360p video+audio, `mime=video/mp4`) — and Google
serves those erratically.

Measured live against one video, each cell reproduced 4/4:

| Request | itag 18 (combined) | itag 140 (audio-only) |
|---|---|---|
| `Range: bytes=0-` | **403** | **206** |
| `Range: bytes=0-1048575` | 206 | 206 |
| `Range: bytes=100-200` | 206 | 206 |
| `Range: bytes=1048576-2097151` | **403** | — |
| `Range: bytes=5000000-6048575` | **403** | **206** |
| no `Range` | **403** | — |

`Range: bytes=0-` is the open-ended request **every browser `<audio>` element
opens with**, and it was the one guaranteed to fail. Audio playback through a
combined format was therefore impossible by construction. Only ranges inside
the first ~1MB worked, which is exactly why a naive probe made it look healthy.

Verified after deploy: `GET /stream/{id}?audio=true` → `format: m4a`,
`itag=140`, `mime=audio/mp4`, `clen` 132MB vs 180MB for itag 18.

## Contract

- `?audio=true` → `bestaudio[ext=m4a]` → `bestaudio` → (last resort) a combined
  stream, so a URL is still returned rather than a hard failure.
- **Default is unchanged (`audio=false`)**; every existing `<video>` caller
  behaves exactly as before.
- `format` is no longer hardcoded `"mp4"`. It comes from yt-dlp's `ext`, else
  the URL's own `mime` param (`audio/mp4` → `m4a`). Callers set Content-Type
  from it, and announcing the wrong container makes browsers refuse to decode.
- The in-memory URL cache is keyed by **(video_id, audio_only)**. One shared
  slot would return a combined video URL to an audio caller whenever a video
  request cached first — silently recreating the defect this change removes.

## Rules this leaves behind

- **Probe with the consumer's real request shape.** `Range: bytes=0-1024`
  returns 206 on a stream no `<audio>` element can play. Test the open-ended
  `bytes=0-`, or the probe passes on broken code.
- **Read the URL's own params before blaming the consumer.** `itag`, `mime` and
  `clen` in the googlevideo query string say immediately whether you are about
  to hand a video file to an audio element.
- googlevideo URLs embed `ip=` and are IP-locked — extract and fetch from the
  same host, or everything 403s for unrelated reasons.

## Consumers

`music-player` (`apps/api/app/services/youtube.py`) passes `audio=true` as of
its `c229838`; see that repo's `AUDIO_PIPELINE.md`. Any future caller feeding
an `<audio>` element must do the same.
