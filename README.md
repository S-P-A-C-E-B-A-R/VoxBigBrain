# VoxBigBrain

VoxBigBrain is a self-hosted, full-duplex voice interface for a local LLM. It keeps a capable reasoning model such as Qwen at the center of the system rather than replacing it with a speech-native conversational model.

VoxBigBrain intentionally requires login before granting access to the local LLM or LiveKit agent session. This protects the inference server and connected tools from unauthorized Internet use.

## ⚠️ Disclaimer

Everything below (and in this repo) is unapologetically vibecoded. Expect vibes, not guarantees. Proceed with good humor and version control.

## What It Does

- Provides browser and iPad-friendly two-way voice conversations over LiveKit.
- Uses Silero VAD to gate low-information audio before faster-whisper streaming STT inference.
- Sends text to a local OpenAI-compatible Qwen endpoint and speaks replies through Kokoro.
- Supports interruption, false-interruption recovery, streamed transcripts, agent state indicators, and text input/output.
- Exposes the Kagi MCP `kagi_search_fetch` tool with privacy-preserving lifecycle telemetry.

VoxBigBrain is experimental software. It is not affiliated with LiveKit, Silero, faster-whisper, Qwen, Kokoro, or Kagi.

## Architecture

```text
Browser / iPad / remote client
          |
          v
   LiveKit signaling and media
          |
          v
     Silero VAD (shared configuration)
          |
          v
  custom streaming STT -> faster-whisper WebSocket
          |
          v
       Qwen-compatible LLM <---- Kagi MCP / future narrow tools
          |
          v
          Kokoro TTS
          |
          v
       LiveKit -> client
```

The voice agent, Whisper, and Kokoro use Docker-internal DNS. Qwen remains an external OpenAI-compatible endpoint configured by environment variable.

## Components

- **LiveKit**: self-hosted rooms, signaling, ICE, and TURN.
- **Silero VAD**: speech detection for STT gating and barge-in state.
- **faster-whisper**: CPU OpenAI-compatible transcription server with a persistent Hugging Face cache and live WebSocket path.
- **Qwen**: local reasoning layer through an OpenAI-compatible API.
- **Kokoro**: CPU text-to-speech server.
- **Kagi MCP**: optional web search through a single allow-listed tool.
- **Web UI**: Express server and static LiveKit browser client.

## Repository Layout

```text
.
├── docker-compose.yml
├── .env.sample
├── source/
│   ├── voice-agent/       # Python LiveKit Agent
│   └── web-ui/            # Express server and browser UI
└── LICENSE
```

## Requirements

- Docker Engine with Docker Compose v2.
- A public DNS and reverse proxy setup if remote browser access is required.
- A reachable OpenAI-compatible Qwen endpoint.
- A Kagi API key only when Kagi MCP search is desired.
- CPU resources appropriate for faster-whisper and Kokoro.

## Quick Start

```sh
cp .env.sample .env
# Edit .env with your DNS names, LiveKit credentials, and Qwen endpoint.
docker compose up -d --build
```

Create the first account after the web service starts:

```sh
docker compose exec web-ui npm run user:add -- alice
```

The command prompts for a password and does not accept it on the command line. Use `npm run user:list` to list accounts and `npm run user:disable -- <username>` to disable an account and revoke its sessions.

Check the web service with `curl http://127.0.0.1:8088/health` and follow the custom service logs with `docker compose logs -f voice-agent web-ui`.

## Environment Configuration

`.env.sample` is the canonical public template. Copy it to the untracked `.env`; never commit that file.

Required deployment settings include `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `PUBLIC_UI_HOST`, `PUBLIC_RTC_HOST`, `PUBLIC_TURN_HOST`, `QWEN_BASE_URL`, `QWEN_API_KEY`, `KAGI_API_KEY`, `SESSION_SECRET`, and `INTERNAL_AGENT_SECRET`. Generate the two authentication secrets with `openssl rand -base64 48`. The Compose file fails clearly when required values are absent. `KAGI_API_KEY` is only needed because the current agent enables Kagi MCP at startup.

The Whisper model, Qwen model, Kokoro voice, timezone, VAD thresholds, and interruption settings are all documented in `.env.sample` and can be adjusted without rebuilding the image. `INTERRUPTION_TRANSCRIPTION_SETTLE_SECONDS=60` is the upper bound for preserving a provisional interruption while a CPU streaming-Whisper final is pending; keep it at least as large as `WHISPER_WS_FINALIZATION_SECONDS`. `INTERRUPTION_CONFIRM_FINAL_ONLY=true` prevents interim text from permanently cancelling speech.

## Voice Agent Behavior

The built-in voice prompt frames VoxBigBrain as a conversational companion rather than a formal request queue. It follows the user's lead, adapts its technicality and energy, remembers relevant current-conversation context, and can react or continue a thought without forcing every exchange into a question-and-answer pattern.

Responses are optimized for speech: direct and fluid by default, concise unless the topic calls for depth, without Markdown, tables, code formatting, long lists, or spoken URLs unless requested. The agent avoids canned capability summaries, repeated offers of help, and generic closing questions.

The current local date and time are supplied to each session and are authoritative for relative-time reasoning. Kagi search is used for explicit searches, current or changing information, or when verification materially improves the answer; results are synthesized and recent credible sources are preferred. The agent cannot control devices, execute commands, access arbitrary files, operate Home Assistant, publish MQTT messages, or make arbitrary HTTP requests.

## Streaming Whisper

`WHISPER_STREAMING_ENABLED=true` is the default. The voice agent opens `WHISPER_WS_URL` when Silero confirms speech, downmixes when necessary, resamples to 16 kHz, and sends only signed 16-bit little-endian mono PCM frames. It publishes the server's cumulative LocalAgreement text as interim LiveKit events and emits the last cumulative result as the final event. The browser replaces one transcript bubble rather than adding one per update.

The deployed `fedirz/faster-whisper-server` WebSocket endpoint is `/v1/audio/transcriptions`. It supports only `model`, `language`, `response_format`, `temperature`, and `vad_filter` query parameters. `WHISPER_TEMPERATURE=0.0` is sent explicitly. The endpoint hard-codes `condition_on_previous_text=false` and LocalAgreement; it does not expose `no_speech_threshold` or LocalAgreement controls, so no unused no-speech setting exists in this project.

faster-whisper-server's live endpoint is not true streaming: after `max_no_data_seconds` (hard-coded 1.0s) of silence it re-transcribes the accumulated buffer and only then sends the finalized JSON. On this CPU host each pass costs roughly 2.4s per second of audio, so the final result arrives a few seconds after you stop speaking and interim updates generally do not appear during short utterances. The adapter therefore waits for that flush instead of closing early (adaptive upper bound `WHISPER_WS_FINALIZATION_SECONDS=60`), and the overall turn latency is similar to the HTTP fallback. If a later utterance arrives while the previous finalize is still pending, the agent finishes the pending flush first before opening the next stream.

Silero remains the authoritative speech gate, turn detector, and interruption signal. `WHISPER_VAD_FILTER=false` is the conservative default because enabling faster-whisper's additional VAD can independently filter/resegment audio during live decoding. The server also has an unavoidable live-stream idle finalization timeout; the adapter never sends silence merely to trigger it. To compare the optional residual filter, set `WHISPER_VAD_FILTER=true`, recreate only `voice-agent`, and compare silence hallucinations, first-word loss, short commands, Bluetooth/car audio, and first-interim/final latency against `false`. Keep the setting that improves those measurements in the target environment.

Set `WHISPER_STREAMING_ENABLED=false` to restore the previous OpenAI-compatible HTTP STT wrapped in LiveKit `StreamAdapter`; Silero gating remains enabled in that rollback path.

## Turn Commit Diagnostics

VoxBigBrain normally uses its `FasterWhisperLiveSTT` WebSocket adapter, not the HTTP fallback. Each Silero-approved speech segment opens one faster-whisper WebSocket request, waits for the server's CPU-backed finalization response at segment end, then emits its final STT event. The adapter serializes its own requests: it finishes a pending segment before opening the next one. The fallback path is the non-streaming OpenAI-compatible `openai.STT` wrapped by LiveKit `StreamAdapter`, where each VAD-ended segment results in one `recognize()` request.

LiveKit Agents 1.7.1 enables preemptive generation by default. In VAD turn-detection mode, its audio-recognition code can invoke that generation path as soon as a final STT fragment arrives, before normal endpointing completes. VoxBigBrain explicitly disables it with the 1.7.1-supported session option `turn_handling={"preemptive_generation": {"enabled": False}}`. No environment variable is used because production behavior should remain explicitly false.

`TURN_TIMELINE` logs use a process-monotonic timestamp and omit transcript text. They cover VAD speech state, faster-whisper segment start/end/finalization and segment IDs, final/interim STT events, committed user and assistant items, speech-generation creation, and agent state changes. A natural pause can still produce multiple VAD/STT segments; the logs distinguish whether those finals became separate committed user turns. These diagnostics are intended to determine whether CPU STT latency or segmentation causes a split before changing VAD values.

## Docker Compose

Build just the custom images:

```sh
docker compose build voice-agent web-ui
```

Recreate only the agent after an environment-only change:

```sh
docker compose up -d --no-deps --force-recreate voice-agent
```

The web UI serves plain HTTP on host port `8088`. TLS belongs at the external reverse proxy, not in this container.

## Authentication And Accounts

Authentication is mandatory. A visitor can access only the login page, its static assets, and the minimal `GET /health` response. Every application API, saved-chat endpoint, browser asset for the voice application, and `POST /api/voice/session` requires an authenticated server-side session.

- Passwords are stored as Argon2id hashes. Plaintext passwords, session tokens, and hashes are never logged.
- Sessions use opaque, HttpOnly, SameSite=Lax cookies. Cookies are marked Secure when `NODE_ENV=production`; terminate TLS at the reverse proxy in Internet-facing deployments.
- Session lifetime defaults to 168 hours. Logout revokes the server-side session immediately.
- State-changing browser requests require a per-session CSRF token and same-origin validation.
- Login failures are generic. Login attempts are limited by source IP plus username. Voice-session creation is limited per authenticated user and defaults to two concurrent active sessions.
- `AUTH_ENABLED` is fixed to `true` by Compose. Anonymous mode is not supported.
- There is no public registration route. Create accounts locally with the admin command above.

### Creating A New Account

On the Docker host, run the account command inside the running `web-ui` container. `--` tells `npm` that everything after it is passed to `node user-admin.mjs`, so append the username there:

```sh
docker compose exec web-ui npm run user:add -- alice
```

The command never accepts the password on the command line: it prompts interactively and masks input (`hideEchoBack`). The username must be 3-32 characters using letters, numbers, dot, underscore, or hyphen; the password must be 12-256 characters. It is stored as an Argon2id hash.

Use `npm run user:list` (same invocation pattern) to list accounts and `npm run user:disable -- <username>` to disable an account and revoke its active sessions.

Relevant optional settings are `SESSION_TTL_HOURS=168`, `LOGIN_RATE_LIMIT_ATTEMPTS=10`, `LOGIN_RATE_LIMIT_WINDOW_MINUTES=15`, `MAX_ACTIVE_VOICE_SESSIONS_PER_USER=2`, `VOICE_SESSION_RATE_LIMIT_PER_MINUTE=10`, and `VOICE_SESSION_TTL_MINUTES=10`. Browser page close sends a best-effort authenticated session-end request; the short voice-session TTL recovers a slot if that request cannot complete.

## Chat Retention

New conversations autosave after their first meaningful finalized user or assistant turn. Empty sessions do not create saved-chat entries. Raw audio, VAD data, interim Whisper fragments, tool inputs, tool results, and interrupted or duplicate turns are not saved.

Finalized user/assistant turns append automatically to SQLite. Saved chats are scoped to the authenticated user; knowing a conversation ID is not sufficient to read it. **Saved Chats** loads the transcript and the next voice session restores the most recent `CHAT_CONTEXT_MAX_MESSAGES` (default 50) in chronological order into LiveKit Agents `ChatContext`, so Qwen receives actual prior context. Long-context summarization is a future enhancement.

Open **Saved Chats** to resume, rename, or permanently delete one of your saved conversations. Rename trims surrounding whitespace and accepts titles up to 120 characters. **New Chat** starts fresh context; its first finalized turn creates a separate saved conversation. Delete permanently removes the selected conversation and its messages. If the deleted chat is active, the live session continues without restoring the deleted history and its next finalized turn starts a new autosaved conversation.

SQLite is stored at `/data/voxbigbrain.db` in the persistent `voxbigbrain-data` Compose volume. To make a consistent simple backup, stop the web service first, then archive the volume:

```sh
docker compose stop web-ui
docker run --rm -v llm_voxbigbrain-data:/data -v "$PWD":/backup alpine tar czf /backup/voxbigbrain-data-backup.tgz -C /data .
docker compose start web-ui
```

Adjust `llm_voxbigbrain-data` if your Compose project name differs. Keep backups private: they contain password hashes and saved transcripts. Schema migrations run at web-service startup, are versioned in SQLite, and never drop data automatically.

## Public Hostnames And Reverse Proxy

Configure three public roles using your own domain:

- `voice.<domain>`: reverse proxy to the web UI at `http://<docker-host>:8088`.
- `rtc.<domain>`: LiveKit signaling/API, normally proxied to LiveKit TCP port `7880` with WebSocket support.
- `turn.<domain>`: TURN hostname advertised by LiveKit.

Use TLS at the proxy for `voice` and `rtc`. Do not terminate TURN UDP traffic at an HTTP proxy.

## WebRTC, TURN, And Firewall

Forward these ports from the public network to the Docker host:

- TCP `7881`: ICE/TCP.
- UDP `7882`: media.
- UDP `443`: TURN.

Port `7880` is also published for LiveKit signaling/API. Confirm public DNS, reverse-proxy WebSocket support, firewall rules, and NAT behavior before debugging browser media.

## iOS, iPad, And Safari

The UI uses `100dvh`, safe-area padding, plain browser controls, `playsInline`, and a user gesture before audio starts. It requests echo cancellation, noise suppression, automatic gain control, and browser voice isolation where available. During an authenticated active voice session it uses the standard Screen Wake Lock API, the supported browser strategy for preventing screen sleep. It releases that lock when the session ends.

On `visibilitychange`, `freeze`, `resume`, `pageshow`, LiveKit reconnect, and local-audio-silence events, the UI shows **Reconnecting...** or **Restoring microphone...**, reacquires the Wake Lock, verifies the microphone track, and re-publishes it through LiveKit without adding duplicate microphone tracks. If the transport cannot reconnect, it ends the old authenticated session and creates a fresh authenticated LiveKit session. Voice-session expiry still follows `VOICE_SESSION_TTL_MINUTES`.

Locked/backgrounded iOS microphone capture is platform-dependent and is not guaranteed by a web application, even with Wake Lock. Test the target iPhone/iPad, Safari version, car integration, and Bluetooth device because media-autoplay and Bluetooth behavior vary by OS release. A native iOS client is the future option when reliable background capture is required.

## VAD And Interruption Tuning

Whisper-family models can hallucinate plausible phrases from silence or noisy, low-information audio. VoxBigBrain uses Silero VAD before faster-whisper inference so non-speech is discarded rather than transcribed.

Default VAD values:

- `VAD_ACTIVATION_THRESHOLD=0.65`
- `VAD_MIN_SPEECH_DURATION=0.20`
- `VAD_MIN_SILENCE_DURATION=0.80`
- `VAD_PREFIX_PADDING_DURATION=0.20`

Default interruption values:

- `INTERRUPTION_MIN_DURATION=0.40`
- `INTERRUPTION_MIN_WORDS=0`
- `FALSE_INTERRUPTION_TIMEOUT=0.80`
- `INTERRUPTION_TRANSCRIPTION_SETTLE_SECONDS=60`
- `INTERRUPTION_CONFIRM_FINAL_ONLY=true`

LiveKit's native two-phase handling uses Silero VAD only to provisionally pause playout. It resumes that paused handle after `FALSE_INTERRUPTION_TIMEOUT` when no committed turn appears, and permanently cancels it only when a non-empty FINAL STT transcript (or a committed reply turn) arrives. The tracker emits compact `INTERRUPTION candidate`, `waiting_for_transcription`, `confirmed`, `false`, `confirmation_timeout`, `speech_cancelled`, and `speech_resumed` diagnostics without logging transcript contents. Empty or interim results do not cancel speech. Finalized user turns and assistant turns that actually played are the only messages eligible for saved-chat persistence; provisional, empty, duplicated, and interrupted tails are excluded.

Increase activation threshold or minimum speech duration to reject more noise. `VAD_MIN_SILENCE_DURATION=0.80` is sized to retain the observed roughly 0.75-second natural pause as one VAD segment; it adds 0.30 seconds to endpointing versus the previous default. Keep short-utterance testing in mind when tuning: commands such as "stop", "no", and "wait" must still be recognized. The same configured Silero VAD settings are used by the streaming STT gate and AgentSession so speech gating and barge-in use consistent settings. The adapter does not submit silent frames to Whisper.

## Kagi MCP And Tool Telemetry

Set `KAGI_API_KEY` in `.env`. The agent only exposes `kagi_search_fetch` through the hosted Kagi MCP service. Tool telemetry is sent on the LiveKit text topic `llm.tool_status` and the UI displays generic start/end status in Event Log only. It excludes tool arguments, user queries, result bodies, and secrets.

## Transcript Behavior

The browser consumes `lk.transcription` incrementally rather than waiting for the complete stream. Interim and final streams share `lk.segment_id`; the UI keys bubbles by participant identity plus segment ID so finals replace their interim bubble instead of duplicating it. Agent state comes from the `lk.agent.state` participant attribute, not inferred from local timing.

## Troubleshooting

- `docker compose config --quiet` verifies environment interpolation and Compose syntax without exposing values.
- `docker compose logs -f voice-agent` shows startup VAD/interruption settings, effective streaming/temperature/VAD-filter configuration, LiveKit registration, compact STT segment metrics, and compact `INTERRUPTION` state transitions (without transcript content).
- If live transcription fails, set `WHISPER_STREAMING_ENABLED=false` and recreate only `voice-agent` to return to the known HTTP fallback while inspecting Whisper service logs.
- `curl http://127.0.0.1:8088/health` checks the web UI service.
- Verify `/v1/models` on the configured Qwen endpoint from the Docker host if the agent cannot answer.
- If remote media fails while UI connection succeeds, inspect UDP/TURN forwarding before changing application code.

## Security Considerations

Keep `.env`, TLS private keys, authentication secrets, and any deployment certificates out of version control. The LiveKit API secret remains server-side; the legacy unauthenticated `/token` endpoint does not exist. `POST /api/voice/session` derives the runtime participant identity from the authenticated server-side user, checks rate/concurrency limits, and then mints a room-scoped token. The Python agent uses a Docker-internal, secret-authenticated interface to retrieve only that session's saved context and to submit finalized turns.

The reverse-proxy configuration is operator-managed outside this repository. Add this rule to the public VoxBigBrain virtual host before its normal application proxy location so public requests never reach the internal agent API:

```nginx
location ^~ /internal/ {
    return 404;
}
```

Keep `INTERNAL_AGENT_SECRET` enabled after adding the proxy rule. The proxy block prevents public routing; the application secret continues to authenticate trusted Docker-network calls.

Use HTTPS at the reverse proxy for the web UI and ensure it forwards the original protocol so Secure cookies work. LiveKit signaling, TURN, and media remain public network services by design, but they require a token issued by the authenticated web service before a browser can join an agent room. This is small self-hosted authentication, not enterprise-grade identity infrastructure. Review any future MCP tool before enabling it.

## Development

Run `python -m compileall -q app` and `python -m unittest discover -s test -v` from `source/voice-agent` for syntax, interruption-state, and streaming-adapter checks. Run `npm test`, `node --check public/app.js`, `node --check public/voice-session.js`, and `node --check server.mjs` from `source/web-ui`. Rebuild the custom images after source changes.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the lightweight contribution guidelines.

## License

VoxBigBrain is source-available under the PolyForm Noncommercial License 1.0.0. Noncommercial use, modification, and redistribution are permitted according to that license. Commercial use requires separate permission or a separate license from the project owner. PolyForm Noncommercial is not an OSI-approved open-source license; see [LICENSE](LICENSE) for the governing terms.

## Project Status

This is an experimental, vibecoded project. Treat it as a starting point, validate it in your own network, and do not rely on it for safety-critical, security-critical, or production customer-facing use without independent review.
