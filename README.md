# VoxBigBrain

VoxBigBrain is a self-hosted, full-duplex voice interface for a local LLM. It keeps a capable reasoning model such as Qwen at the center of the system rather than replacing it with a speech-native conversational model.

VoxBigBrain intentionally requires login before granting access to the local LLM or LiveKit agent session. This protects the inference server and connected tools from unauthorized Internet use.

## ⚠️ Disclaimer

Everything below (and in this repo) is unapologetically vibecoded. Expect vibes, not guarantees. Proceed with good humor and version control.

## What It Does

- Provides browser and iPad-friendly two-way voice conversations over LiveKit.
- Uses Silero VAD to gate low-information audio before faster-whisper STT inference.
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
 StreamAdapter -> faster-whisper STT
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
- **faster-whisper**: CPU OpenAI-compatible transcription server with a persistent Hugging Face cache.
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

The Whisper model, Qwen model, Kokoro voice, timezone, VAD thresholds, and interruption settings are all documented in `.env.sample` and can be adjusted without rebuilding the image.

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

Relevant optional settings are `SESSION_TTL_HOURS=168`, `LOGIN_RATE_LIMIT_ATTEMPTS=10`, `LOGIN_RATE_LIMIT_WINDOW_MINUTES=15`, `MAX_ACTIVE_VOICE_SESSIONS_PER_USER=2`, and `VOICE_SESSION_RATE_LIMIT_PER_MINUTE=10`.

## Chat Retention

New conversations are ephemeral by default. Finalized turns stay only in runtime memory and disappear after disconnect/restart unless the signed-in user selects **Save Chat**. Raw audio, VAD data, interim Whisper fragments, tool inputs, and tool results are not saved.

Saving promotes the current finalized history and subsequent finalized user/assistant turns to SQLite. Saved chats are scoped to the authenticated user; knowing a conversation ID is not sufficient to read it. **Saved Chats** loads the transcript and the next voice session restores the most recent `CHAT_CONTEXT_MAX_MESSAGES` (default 50) in chronological order into LiveKit Agents `ChatContext`, so Qwen receives actual prior context. Long-context summarization is a future enhancement.

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

The UI uses `100dvh`, safe-area padding, plain browser controls, `playsInline`, and a user gesture before audio starts. It requests echo cancellation, noise suppression, automatic gain control, and browser voice isolation where available. Test on the target iPad/Safari version because media-autoplay and Bluetooth behavior vary by OS release.

## VAD And Interruption Tuning

Whisper-family models can hallucinate plausible phrases from silence or noisy, low-information audio. VoxBigBrain uses Silero VAD before faster-whisper inference so non-speech is discarded rather than transcribed.

Default VAD values:

- `VAD_ACTIVATION_THRESHOLD=0.65`
- `VAD_MIN_SPEECH_DURATION=0.20`
- `VAD_MIN_SILENCE_DURATION=0.50`
- `VAD_PREFIX_PADDING_DURATION=0.20`

Default interruption values:

- `INTERRUPTION_MIN_DURATION=0.50`
- `INTERRUPTION_MIN_WORDS=1`
- `FALSE_INTERRUPTION_TIMEOUT=0.80`

Increase activation threshold or minimum speech duration to reject more noise. Keep short-utterance testing in mind when tuning: commands such as "stop", "no", and "wait" must still be recognized. The same configured Silero VAD object is shared by the STT StreamAdapter and AgentSession so speech gating and barge-in use consistent settings.

## Kagi MCP And Tool Telemetry

Set `KAGI_API_KEY` in `.env`. The agent only exposes `kagi_search_fetch` through the hosted Kagi MCP service. Tool telemetry is sent on the LiveKit text topic `llm.tool_status` and the UI displays generic start/end status in Event Log only. It excludes tool arguments, user queries, result bodies, and secrets.

## Transcript Behavior

The browser consumes `lk.transcription` incrementally rather than waiting for the complete stream. Interim and final streams share `lk.segment_id`; the UI keys bubbles by participant identity plus segment ID so finals replace their interim bubble instead of duplicating it. Agent state comes from the `lk.agent.state` participant attribute, not inferred from local timing.

## Troubleshooting

- `docker compose config --quiet` verifies environment interpolation and Compose syntax without exposing values.
- `docker compose logs -f voice-agent` shows startup VAD/interruption settings, LiveKit registration, and STT segment metrics.
- `curl http://127.0.0.1:8088/health` checks the web UI service.
- Verify `/v1/models` on the configured Qwen endpoint from the Docker host if the agent cannot answer.
- If remote media fails while UI connection succeeds, inspect UDP/TURN forwarding before changing application code.

## Security Considerations

Keep `.env`, TLS private keys, authentication secrets, and any deployment certificates out of version control. The LiveKit API secret remains server-side; the legacy unauthenticated `/token` endpoint does not exist. `POST /api/voice/session` derives the runtime participant identity from the authenticated server-side user, checks rate/concurrency limits, and then mints a room-scoped token. The Python agent uses a Docker-internal, secret-authenticated interface to retrieve only that session's saved context and to submit finalized turns.

Use HTTPS at the reverse proxy for the web UI and ensure it forwards the original protocol so Secure cookies work. LiveKit signaling, TURN, and media remain public network services by design, but they require a token issued by the authenticated web service before a browser can join an agent room. This is small self-hosted authentication, not enterprise-grade identity infrastructure. Review any future MCP tool before enabling it.

## Development

Run `python -m compileall -q app` from `source/voice-agent` for a quick Python syntax check. Run `npm test`, `node --check public/app.js`, and `node --check server.mjs` from `source/web-ui`. Rebuild the custom images after source changes.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the lightweight contribution guidelines.

## License

VoxBigBrain is source-available under the PolyForm Noncommercial License 1.0.0. Noncommercial use, modification, and redistribution are permitted according to that license. Commercial use requires separate permission or a separate license from the project owner. PolyForm Noncommercial is not an OSI-approved open-source license; see [LICENSE](LICENSE) for the governing terms.

## Project Status

This is an experimental, vibecoded project. Treat it as a starting point, validate it in your own network, and do not rely on it for safety-critical, security-critical, or production customer-facing use without independent review.
