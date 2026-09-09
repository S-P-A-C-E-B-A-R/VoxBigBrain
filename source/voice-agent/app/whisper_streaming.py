"""LiveKit STT adapter for fedirz/faster-whisper-server's PCM WebSocket API."""

import asyncio
import json
import logging
import time
from array import array
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp
from livekit import rtc
from livekit.agents import stt, vad
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NOT_GIVEN, NotGivenOr

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WhisperLiveOptions:
    url: str
    model: str
    language: str
    temperature: float
    vad_filter: bool
    # Upper bound (s) for waiting on the server's finalization flush. The server
    # waits `max_no_data_seconds` (hard-coded 1.0) of silence, then re-transcribes
    # the accumulated buffer before sending the final result.
    finalization_seconds: float = 60.0
    # Server-side transcribe latency factor observed on CPU (~2.4s per 1s of audio).
    transcribe_latency_factor: float = 2.5
    # Floor (s) so even a one-word utterance leaves enough time for the flush pass.
    min_finalization_seconds: float = 4.0

    def websocket_url(self) -> str:
        query = urlencode({
            "model": self.model,
            "language": self.language,
            "response_format": "json",
            "temperature": str(self.temperature),
            "vad_filter": str(self.vad_filter).lower(),
        })
        return f"{self.url}?{query}"


class FasterWhisperLiveSTT(stt.STT):
    """Streams Silero-approved PCM to faster-whisper and emits cumulative text."""

    def __init__(self, *, options: WhisperLiveOptions, speech_vad: vad.VAD) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=True, offline_recognize=False))
        self._options = options
        self._vad = speech_vad

    @property
    def model(self) -> str:
        return self._options.model

    @property
    def provider(self) -> str:
        return "faster-whisper-websocket"

    async def _recognize_impl(self, *args, **kwargs) -> stt.SpeechEvent:
        raise NotImplementedError("faster-whisper live STT is streaming-only")

    def stream(self, *, language: NotGivenOr[str] = NOT_GIVEN, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> stt.RecognizeStream:
        selected_language = self._options.language if language is NOT_GIVEN else language
        return _FasterWhisperLiveStream(self, self._vad, self._options, selected_language, conn_options)


class _FasterWhisperLiveStream(stt.RecognizeStream):
    def __init__(self, parent: FasterWhisperLiveSTT, speech_vad: vad.VAD, options: WhisperLiveOptions, language: str, conn_options: APIConnectOptions) -> None:
        super().__init__(stt=parent, conn_options=conn_options, sample_rate=16000)
        self._speech_vad = speech_vad
        self._options = options
        self._language = language
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._last_text = ""
        self._speech_audio_duration = 0.0
        self._first_interim_at: float | None = None
        self._utterance_started_at: float | None = None
        self._interim_updates = 0
        self._segment_sequence = 0
        self._segment_id = ""

    def _finalization_wait(self) -> float:
        """How long to wait for the server's finalization flush before closing.

        The server hard-codes a 1.0s no-data timeout (config max_no_data_seconds),
        then re-transcribes the accumulated buffer before sending the final JSON.
        Wait for that flush instead of racing it; the wait ends early if the server
        closes the socket itself after sending the result.
        """
        estimate = 1.5 + self._speech_audio_duration * self._options.transcribe_latency_factor
        return min(self._options.finalization_seconds, max(self._options.min_finalization_seconds, estimate))

    def push_frame(self, frame: rtc.AudioFrame) -> None:
        # Browser microphones are normally mono. Downmix before the base class resamples.
        if frame.num_channels > 1:
            samples = array("h")
            samples.frombytes(bytes(frame.data))
            mono = array("h", (int(sum(samples[index:index + frame.num_channels]) / frame.num_channels) for index in range(0, len(samples), frame.num_channels)))
            frame = rtc.AudioFrame(mono.tobytes(), frame.sample_rate, 1, frame.samples_per_channel)
        super().push_frame(frame)

    async def _run(self) -> None:
        gate = self._speech_vad.stream()
        try:
            feeder = asyncio.create_task(self._feed_gate(gate), name="whisper-live-vad-feed")
            async for event in gate:
                await self._handle_vad_event(event)
            await feeder
        finally:
            await self._finish_utterance()
            await gate.aclose()

    async def _feed_gate(self, gate: vad.VADStream) -> None:
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                gate.flush()
            else:
                gate.push_frame(item)

    async def _handle_vad_event(self, event: vad.VADEvent) -> None:
        if event.type == vad.VADEventType.START_OF_SPEECH:
            await self._finish_utterance()
            await self._start_utterance()
            await self._send_frames(event.frames)
            self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH, request_id=self._segment_id))
        elif event.type == vad.VADEventType.INFERENCE_DONE and event.speaking:
            await self._send_frames(event.frames)
        elif event.type == vad.VADEventType.END_OF_SPEECH:
            if self._ws is None:
                return
            logger.info("TURN_TIMELINE t=%.6f event=stt_segment_end segment=%s", time.monotonic(), self._segment_id)
            self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH, request_id=self._segment_id))
            # The server finalizes LocalAgreement after an idle interval. No silent PCM is sent.
            await self._finish_utterance()

    async def _start_utterance(self) -> None:
        self._last_text = ""
        self._speech_audio_duration = 0.0
        self._first_interim_at = None
        self._utterance_started_at = time.monotonic()
        self._interim_updates = 0
        self._segment_sequence += 1
        self._segment_id = f"{id(self):x}-{self._segment_sequence}"
        logger.info("TURN_TIMELINE t=%.6f event=stt_segment_start segment=%s", self._utterance_started_at, self._segment_id)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10.0)
        self._http = aiohttp.ClientSession(timeout=timeout)
        self._ws = await self._http.ws_connect(self._options.websocket_url())
        self.start_time = time.time()
        self._receive_task = asyncio.create_task(self._receive(self._ws), name="whisper-live-receive")

    async def _send_frames(self, frames: list[rtc.AudioFrame]) -> None:
        if self._ws is None:
            return
        for frame in frames:
            payload = bytes(frame.data)
            if not payload:
                continue
            await self._ws.send_bytes(payload)
            self._speech_audio_duration += frame.samples_per_channel / frame.sample_rate

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                response = json.loads(message.data)
                raw_text = response["text"].strip()
            except (TypeError, ValueError, KeyError):
                logger.warning("Whisper live response was malformed")
                continue
            # The server joins word tokens with " " while tokens already carry
            # leading spaces (" hello", " world"), producing doubled spacing.
            text = " ".join(raw_text.split())
            if not text or text == self._last_text:
                continue
            self._last_text = text
            self._interim_updates += 1
            if self._first_interim_at is None:
                self._first_interim_at = time.monotonic()
            # TODO: compare interim user text with active TTS text for future echo rejection.
            self._event_ch.send_nowait(self._speech_event(stt.SpeechEventType.INTERIM_TRANSCRIPT, text))

    def _speech_event(self, event_type: stt.SpeechEventType, text: str) -> stt.SpeechEvent:
        return stt.SpeechEvent(type=event_type, request_id=self._segment_id, alternatives=[stt.SpeechData(language=self._language, text=text)])

    async def _finish_utterance(self) -> None:
        if self._ws is None:
            return
        ws, receive_task, http = self._ws, self._receive_task, self._http
        self._ws = self._receive_task = self._http = None
        try:
            # This server finalizes after `max_no_data_seconds` (1.0s) of silence,
            # flushing its accumulated transcription. Do not send silence; just wait
            # for the flush (or the socket close) up to the adaptive deadline.
            await asyncio.wait_for(receive_task, timeout=self._finalization_wait())
        except asyncio.TimeoutError:
            logger.warning("Whisper live finalization timed out")
        except Exception:
            logger.exception("Whisper live receive failed")
        finally:
            await ws.close()
            await http.close()
        if self._last_text:
            self._event_ch.send_nowait(self._speech_event(stt.SpeechEventType.FINAL_TRANSCRIPT, self._last_text))
        logger.info(
            "TURN_TIMELINE t=%.6f event=stt_finalized segment=%s audio_duration=%.2f chars=%d final=%s",
            time.monotonic(),
            self._segment_id,
            self._speech_audio_duration,
            len(self._last_text),
            bool(self._last_text),
        )
        first_interim = 0.0 if self._first_interim_at is None else self._first_interim_at - self._utterance_started_at
        logger.info("Whisper live segment audio_duration=%.2fs first_interim=%.2fs interim_updates=%d final=%s", self._speech_audio_duration, first_interim, self._interim_updates, bool(self._last_text))
