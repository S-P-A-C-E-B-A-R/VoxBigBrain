import asyncio
import dataclasses
import os
import struct
import unittest
from unittest.mock import patch

from aiohttp import web
from livekit import rtc
from livekit.agents import vad

from app.whisper_streaming import FasterWhisperLiveSTT, WhisperLiveOptions

for name in ("WHISPER_BASE_URL", "WHISPER_MODEL", "KOKORO_BASE_URL", "KOKORO_MODEL", "KOKORO_VOICE", "QWEN_BASE_URL", "QWEN_API_KEY", "QWEN_MODEL", "KAGI_API_KEY", "INTERNAL_AGENT_SECRET"):
    os.environ.setdefault(name, "test")
from app import agent


class FakeVADStream:
    def __init__(self, speech=True):
        self.events = asyncio.Queue()
        self.started = False
        self.speech = speech

    def push_frame(self, frame):
        if self.speech and not self.started:
            self.started = True
            self.events.put_nowait(vad.VADEvent(type=vad.VADEventType.START_OF_SPEECH, samples_index=0, timestamp=0, speech_duration=0, silence_duration=0, frames=[frame], speaking=True))
        elif self.speech:
            self.events.put_nowait(vad.VADEvent(type=vad.VADEventType.INFERENCE_DONE, samples_index=0, timestamp=0, speech_duration=0, silence_duration=0, frames=[frame], speaking=True))

    def flush(self):
        self.events.put_nowait(vad.VADEvent(type=vad.VADEventType.END_OF_SPEECH, samples_index=0, timestamp=0, speech_duration=0, silence_duration=0, speaking=False))
        self.events.put_nowait(None)

    async def aclose(self):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.events.get()
        if event is None:
            raise StopAsyncIteration
        return event


class FakeVAD:
    def __init__(self, speech=True):
        self.streams = []
        self.speech = speech

    def stream(self):
        stream = FakeVADStream(self.speech)
        self.streams.append(stream)
        return stream


class WhisperStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []

        async def live(request):
            self.query = dict(request.query)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            frame_count = 0
            if request.query["model"] == "disconnect":
                await ws.close()
                return ws
            async for message in ws:
                if message.type == web.WSMsgType.BINARY:
                    frame_count += 1
                    self.received.append(message.data)
                    if request.query["model"] == "malformed":
                        await ws.send_str("not json")
                        await ws.close()
                    elif request.query["model"] == "slow":
                        await asyncio.sleep(3.0)
                        await ws.send_json({"text": "hello"})
                        await ws.close()
                    elif request.query["model"] == "spaces":
                        await ws.send_json({"text": "  hello   world "})
                        await ws.close()
                    elif request.query["model"] == "hold":
                        continue
                    else:
                        await ws.send_json({"text": "hello"})
                        await ws.close()
            return ws

        self.app = web.Application()
        self.app.router.add_get("/v1/audio/transcriptions", live)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.options = WhisperLiveOptions(f"ws://127.0.0.1:{port}/v1/audio/transcriptions", "test-model", "en", 0.0, False)

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_streams_pcm_in_order_and_emits_interim_then_final(self):
        adapter = FasterWhisperLiveSTT(options=self.options, speech_vad=FakeVAD())
        self.assertTrue(adapter.capabilities.streaming)
        self.assertTrue(adapter.capabilities.interim_results)
        stream = adapter.stream()
        first_pcm = struct.pack("<hh", 1, -2)
        stream.push_frame(rtc.AudioFrame(first_pcm, 16000, 1, 2))
        stream.end_input()
        events = [event async for event in stream]
        self.assertEqual(self.received, [first_pcm])
        self.assertEqual([event.type.value for event in events if event.alternatives], ["interim_transcript", "final_transcript"])
        self.assertEqual(events[-1].alternatives[0].text, "hello")
        self.assertTrue(events[-1].request_id)
        self.assertEqual(events[-2].request_id, events[-1].request_id)
        self.assertEqual(self.query["temperature"], "0.0")
        self.assertEqual(self.query["vad_filter"], "false")

    async def test_transport_sends_frames_in_order(self):
        adapter = FasterWhisperLiveSTT(options=dataclasses.replace(self.options, model="hold", min_finalization_seconds=0.5), speech_vad=FakeVAD())
        stream = adapter.stream()
        first_pcm, second_pcm = struct.pack("<hh", 1, -2), struct.pack("<hh", 3, -4)
        await stream._start_utterance()
        await stream._send_frames([rtc.AudioFrame(first_pcm, 16000, 1, 2), rtc.AudioFrame(second_pcm, 16000, 1, 2)])
        await stream._finish_utterance()
        self.assertEqual(self.received, [first_pcm, second_pcm])
        await stream.aclose()

    async def test_silence_never_opens_or_submits_to_websocket(self):
        adapter = FasterWhisperLiveSTT(options=self.options, speech_vad=FakeVAD(speech=False))
        stream = adapter.stream()
        stream.push_frame(rtc.AudioFrame(b"\0\0" * 160, 16000, 1, 160))
        stream.end_input()
        self.assertEqual([event async for event in stream], [])
        self.assertEqual(self.received, [])

    async def test_malformed_response_and_disconnect_do_not_crash_stream(self):
        for model in ("malformed", "disconnect"):
            adapter = FasterWhisperLiveSTT(options=dataclasses.replace(self.options, model=model), speech_vad=FakeVAD())
            stream = adapter.stream()
            stream.push_frame(rtc.AudioFrame(b"\1\0", 16000, 1, 1))
            stream.end_input()
            self.assertEqual([event async for event in stream if event.alternatives], [])

    async def test_delayed_finalize_is_not_clipped_by_client_close(self):
        adapter = FasterWhisperLiveSTT(options=dataclasses.replace(self.options, model="slow"), speech_vad=FakeVAD())
        stream = adapter.stream()
        stream.push_frame(rtc.AudioFrame(struct.pack("<hh", 1, -2), 16000, 1, 2))
        stream.end_input()
        events = [event async for event in stream]
        self.assertTrue(events)
        self.assertEqual(events[-1].type.value, "final_transcript")
        self.assertEqual(events[-1].alternatives[0].text, "hello")

    async def test_word_join_double_spaces_are_normalized(self):
        adapter = FasterWhisperLiveSTT(options=dataclasses.replace(self.options, model="spaces"), speech_vad=FakeVAD())
        stream = adapter.stream()
        stream.push_frame(rtc.AudioFrame(struct.pack("<hh", 1, -2), 16000, 1, 2))
        stream.end_input()
        events = [event async for event in stream]
        self.assertTrue(events)
        self.assertEqual(events[-1].alternatives[0].text, "hello world")

    def test_fallback_mode_initializes_stream_adapter(self):
        with patch.object(agent, "config", dataclasses.replace(agent.config, whisper_streaming_enabled=False)):
            self.assertFalse(agent.make_stt(FakeVAD()).capabilities.interim_results)

    def test_turn_handling_disables_preemptive_generation(self):
        self.assertFalse(agent.turn_handling_options()["preemptive_generation"]["enabled"])

    def test_live_options_only_use_supported_query_parameters(self):
        query = self.options.websocket_url().split("?", 1)[1]
        self.assertEqual(set(part.split("=", 1)[0] for part in query.split("&")), {"model", "language", "response_format", "temperature", "vad_filter"})
