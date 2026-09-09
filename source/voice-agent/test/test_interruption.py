import asyncio
import unittest
from types import SimpleNamespace

from app.interruption import InterruptionState, InterruptionTracker


class FakeSpeech:
    def __init__(self, speech_id="speech-1"):
        self.id = speech_id
        self.interrupted = False
        self._done = False

    def done(self):
        return self._done


class FakeSession:
    def __init__(self):
        self.agent_state = "speaking"
        self.user_state = "listening"
        self.current_speech = FakeSpeech()
        self.callbacks = {}
        self.interrupts = 0

    def on(self, event, callback):
        self.callbacks[event] = callback

    def emit(self, event, **kwargs):
        if event == "agent_state_changed":
            self.agent_state = kwargs["new_state"]
        elif event == "user_state_changed":
            self.user_state = kwargs["new_state"]
        self.callbacks[event](SimpleNamespace(**kwargs))

    async def interrupt(self):
        self.interrupts += 1
        self.current_speech.interrupted = True


class FakeItem:
    def __init__(self, role="user", text="hello", interrupted=False):
        self.role = role
        self.text_content = text
        self.interrupted = interrupted
        self.id = "item-1"


class InterruptionTrackerTests(unittest.IsolatedAsyncioTestCase):
    def make_tracker(self, settle=0.02):
        self.session = FakeSession()
        self.tracker = InterruptionTracker(self.session, settle_seconds=settle)

    def candidate(self):
        self.session.user_state = "speaking"
        self.session.emit("user_state_changed", new_state="speaking")

    async def test_vad_candidate_is_provisional(self):
        self.make_tracker()
        self.candidate()
        self.assertEqual(self.tracker.state, InterruptionState.PROVISIONAL_INTERRUPTION)
        self.assertEqual(self.tracker.episode.id, 1)

    async def test_interim_never_confirms_when_final_only(self):
        self.make_tracker()
        self.candidate()
        self.session.emit("user_input_transcribed", transcript="please", is_final=False)
        self.assertEqual(self.tracker.state, InterruptionState.PROVISIONAL_INTERRUPTION)

    async def test_final_confirms_for_streaming_and_http_fallback(self):
        for adapter in ("streaming", "http-fallback"):
            with self.subTest(adapter=adapter):
                self.make_tracker()
                self.candidate()
                self.session.emit("user_input_transcribed", transcript="please stop", is_final=True)
                self.assertEqual(self.tracker.state, InterruptionState.CONFIRMED_INTERRUPTION)
                self.assertEqual(self.tracker.episode.final_chars, len("please stop"))

    async def test_empty_final_does_not_confirm(self):
        self.make_tracker()
        self.candidate()
        self.session.emit("user_input_transcribed", transcript="   ", is_final=True)
        self.assertEqual(self.tracker.state, InterruptionState.PROVISIONAL_INTERRUPTION)

    async def test_settle_timeout_declares_false_without_cancelling(self):
        self.make_tracker()
        self.candidate()
        await asyncio.sleep(0.04)
        self.assertEqual(self.tracker.state, InterruptionState.FALSE_INTERRUPTION)
        self.assertEqual(self.session.interrupts, 0)

    async def test_transcription_timeout_declares_false(self):
        self.make_tracker(settle=1)
        self.candidate()
        self.session.emit("user_transcription_timeout")
        self.assertEqual(self.tracker.state, InterruptionState.FALSE_INTERRUPTION)

    async def test_native_resume_stays_provisional_for_late_final(self):
        self.make_tracker(settle=1)
        self.candidate()
        self.session.emit("agent_false_interruption", resumed=True)
        self.assertEqual(self.tracker.state, InterruptionState.PROVISIONAL_INTERRUPTION)
        self.session.emit("user_input_transcribed", transcript="real request", is_final=True)
        self.assertEqual(self.tracker.state, InterruptionState.CONFIRMED_INTERRUPTION)

    async def test_late_final_after_false_reconfirms_same_episode(self):
        self.make_tracker()
        self.candidate()
        episode_id = self.tracker.episode.id
        await asyncio.sleep(0.04)
        self.session.emit("user_input_transcribed", transcript="slow final", is_final=True)
        self.assertEqual(self.tracker.state, InterruptionState.CONFIRMED_INTERRUPTION)
        self.assertEqual(self.tracker.episode.id, episode_id)

    async def test_cancel_safety_net_runs_only_after_final(self):
        self.make_tracker(settle=1)
        self.candidate()
        await asyncio.sleep(0.02)
        self.assertEqual(self.session.interrupts, 0)
        self.session.emit("user_input_transcribed", transcript="cancel", is_final=True)
        await asyncio.sleep(1.05)
        self.assertEqual(self.session.interrupts, 1)

    async def test_persistence_gate_rejects_empty_and_interrupted_items(self):
        self.make_tracker()
        self.assertTrue(self.tracker.should_persist(FakeItem()))
        self.assertFalse(self.tracker.should_persist(FakeItem(text="")))
        self.assertFalse(self.tracker.should_persist(FakeItem(interrupted=True)))
        self.assertFalse(self.tracker.should_persist(FakeItem(role="system")))

    async def test_normal_assistant_completion_clears_stale_state(self):
        self.make_tracker(settle=1)
        self.tracker.state = InterruptionState.SPEAKING
        self.session.emit("agent_state_changed", old_state="speaking", new_state="listening")
        self.assertEqual(self.tracker.state, InterruptionState.IDLE)
        self.assertIsNone(self.tracker.episode)
        self.assertIsNone(self.tracker._settle_handle)
        self.assertIsNone(self.tracker._verify_handle)

    async def test_normal_next_turn_is_not_an_interruption(self):
        self.make_tracker(settle=1)
        self.tracker.state = InterruptionState.SPEAKING
        self.session.emit("agent_state_changed", old_state="speaking", new_state="listening")
        self.session.emit("user_state_changed", old_state="listening", new_state="speaking")
        self.session.emit("user_input_transcribed", transcript="new question", is_final=True)
        await asyncio.sleep(0.02)
        self.assertEqual(self.tracker.state, InterruptionState.IDLE)
        self.assertIsNone(self.tracker.episode)
        self.assertEqual(self.session.interrupts, 0)

    async def test_assistant_finish_while_user_speaking_preserves_barge_in(self):
        self.make_tracker(settle=1)
        self.candidate()
        self.session.emit("agent_state_changed", old_state="speaking", new_state="listening")
        self.assertEqual(self.tracker.state, InterruptionState.PROVISIONAL_INTERRUPTION)
        self.assertIsNotNone(self.tracker.episode)

    async def test_cleared_episode_cannot_interrupt_new_speech(self):
        self.make_tracker(settle=0.02)
        self.candidate()
        self.session.emit("user_input_transcribed", transcript="stop", is_final=True)
        self.tracker._clear_episode(reason="test_reset")
        self.session.current_speech = FakeSpeech("speech-new")
        await asyncio.sleep(0.04)
        self.assertEqual(self.session.interrupts, 0)

    async def test_repeated_normal_completions_leave_no_episode(self):
        self.make_tracker(settle=1)
        for _ in range(3):
            self.tracker.state = InterruptionState.SPEAKING
            self.session.agent_state = "speaking"
            self.session.emit("agent_state_changed", old_state="speaking", new_state="listening")
            self.assertEqual(self.tracker.state, InterruptionState.IDLE)
            self.assertIsNone(self.tracker.episode)
