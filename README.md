# VoxBigBrain

VoxBigBrain is a self-hosted, full-duplex voice interface for a local LLM. It keeps a capable reasoning model such as Qwen at the center of the system rather than replacing it with a speech-native conversational model.

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

Check the web service with `curl http://127.0.0.1:8088/health` and follow the custom service logs with `docker compose logs -f voice-agent web-ui`.

## Environment Configuration

`.env.sample` is the canonical public template. Copy it to the untracked `.env`; never commit that file.

Required deployment settings include `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `PUBLIC_UI_HOST`, `PUBLIC_RTC_HOST`, `PUBLIC_TURN_HOST`, `QWEN_BASE_URL`, `QWEN_API_KEY`, and `KAGI_API_KEY`. The Compose file fails clearly when required values are absent. `KAGI_API_KEY` is only needed because the current agent enables Kagi MCP at startup.

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

Keep `.env`, TLS private keys, and any deployment certificates out of version control. The web UI can mint room tokens, so protect its network exposure with your reverse proxy and normal access controls. This project intentionally has no shell, filesystem, arbitrary HTTP, MQTT, Home Assistant, or device-control tools. Review any future MCP tool before enabling it.

## Development

Run `python -m compileall -q app` from `source/voice-agent` for a quick Python syntax check. Run `node --check public/app.js` and `node --check server.mjs` from `source/web-ui`. Rebuild the custom images after source changes.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the lightweight contribution guidelines.

## License

VoxBigBrain is source-available under the PolyForm Noncommercial License 1.0.0. Noncommercial use, modification, and redistribution are permitted according to that license. Commercial use requires separate permission or a separate license from the project owner. PolyForm Noncommercial is not an OSI-approved open-source license; see [LICENSE](LICENSE) for the governing terms.

## Project Status

This is an experimental, vibecoded project. Treat it as a starting point, validate it in your own network, and do not rely on it for safety-critical, security-critical, or production customer-facing use without independent review.
