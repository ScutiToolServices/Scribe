# Purpose

**Kind:** audio → text

**Group:** Speech-to-Text

**Tags:** `cpu`, `mcp`, `http`, `websocket`, `streaming`, `ffmpeg`

| | |
|---|---|
| Input | An uploaded audio file (mp3, wav, m4a, ogg, flac or webm; at most 25 MB and 150 s) through `POST /api/files`, or a stream of 16 kHz mono signed 16-bit PCM over the `WS /api/stream` dictation socket. |
| Output | A transcript: `tool_scribe_transcribe` returns text (with timestamps on request); `POST /api/transcribe` returns `{text, duration_s}`; the socket sends `partial` and `final` text. Uploads and transcripts are deleted after 24 hours. |
| Needs | `STTWhisperModel`, one `whisper` connection on the admin page (`STTMoonshineModel` has the same request shape). With none connected, transcribing fails with a hint. |
| Runs on | CPU. Holds no model; ffmpeg decodes uploads to PCM. |

**Use for:** transcribing attached audio and live dictation in Ember or Citadel.
**Not for:** cleaning up the transcript (see `S1MiniModel`), languages other than English with the default model, speaker labels.
