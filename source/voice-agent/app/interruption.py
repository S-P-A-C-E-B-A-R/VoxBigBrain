"""Two-phase interruption confirmation tracker.

LiveKit 1.7.1 natively pauses assistant playout on VAD onset, resumes it after
``false_interruption_timeout`` when no turn commits, and permanently cancels the
paused speech once a FINAL transcript (or a replying turn) arrives. This module
adds a supervisory state machine on top of those public session events so that:

- a VAD onset while the agent speaks only ever marks a PROVISIONAL episode
  (the framework owns pause/resume; nothing here cancels speech early),
- permanent cancellation is confirmed only by a FINAL non-empty transcription,
- false episodes are declared only after the settle window elapses with no
  FINAL transcript (or when transcription explicitly times out),
- a FINAL that arrives after an episode already closed is treated as a late
  final for a new turn, never as a re-confirmation of the old episode,
- every transition emits one compact ``INTERRUPTION ...`` diagnostic line
  without logging transcript contents.

Only public ``AgentSession`` APIs are used (``on``, ``current_speech``,
``agent_state``, ``interrupt``). No private activity/handle machinery is
touched, and the native ``resume_false_interruption`` behavior is left intact.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

# Grace period after a confirming FINAL before verifying the interrupted speech
# is actually cancelled. Lets the framework's own cancel path run first; the
# safety net only fires when the handle is still alive and un-interrupted.
_CANCEL_VERIFY_DELAY_SECONDS = 1.0


class InterruptionState(str, Enum):
    IDLE = "IDLE"
    SPEAKING = "SPEAKING"
    PROVISIONAL_INTERRUPTION = "PROVISIONAL_INTERRUPTION"
    CONFIRMED_INTERRUPTION = "CONFIRMED_INTERRUPTION"
    FALSE_INTERRUPTION = "FALSE_INTERRUPTION"


@dataclass
class InterruptionEpisode:
    id: int
    speech_id: str | None
    started_at: float = field(default_factory=time.time)
    user_eos_at: float | None = None
    final_chars: int = 0
    resumes: int = 0
    outcome: str = "open"


class InterruptionTracker:
    """Mirror VAD/STT/session events into IDLE/SPEAKING/PROVISIONAL/CONFIRMED/FALSE."""

    def __init__(self, session, *, settle_seconds: float = 20.0, confirm_final_only: bool = True) -> None:
        self._session = session
        self._settle_seconds = settle_seconds
        self._confirm_final_only = confirm_final_only
        self.state = InterruptionState.IDLE
        self._episode: InterruptionEpisode | None = None
        self._episode_seq = 0
        self._settle_handle: asyncio.TimerHandle | None = None
        self._verify_handle: asyncio.TimerHandle | None = None
        session.on("agent_state_changed", self._on_agent_state)
        session.on("user_state_changed", self._on_user_state)
        session.on("user_input_transcribed", self._on_transcribed)
        session.on("agent_false_interruption", self._on_false_interruption)
        session.on("user_transcription_timeout", self._on_transcription_timeout)

    # -- public helpers ----------------------------------------------------

    @property
    def episode(self) -> InterruptionEpisode | None:
        return self._episode

    def should_persist(self, item) -> bool:
        """Persistence gate: only finalized user turns and committed assistant turns.

        Mirrors the long-standing hook rules (role user/assistant, non-empty text,
        assistant must not be interrupted) so provisional, empty, or truncated
        speech never lands in saved chats. Returns True when the item may be saved.
        """
        role = getattr(item, "role", None)
        if role not in ("user", "assistant"):
            return False
        if getattr(item, "interrupted", False):
            logger.debug("INTERRUPTION persist_skip interrupted=%s role=%s", getattr(item, "id", "?"), role)
            return False
        text = getattr(item, "text_content", "") or ""
        if not text.strip():
            logger.debug("INTERRUPTION persist_skip empty role=%s", role)
            return False
        return True

    # -- session event handlers --------------------------------------------

    def _on_agent_state(self, event) -> None:
        new_state = getattr(event, "new_state", None)
        if new_state == "speaking":
            if self.state in (InterruptionState.IDLE, InterruptionState.FALSE_INTERRUPTION, InterruptionState.CONFIRMED_INTERRUPTION):
                self.state = InterruptionState.SPEAKING
            # While PROVISIONAL a return to "speaking" is the native resume of the
            # paused handle; the resume itself is logged via agent_false_interruption.
            return
        if new_state in ("listening", "thinking", "idle"):
            if self.state == InterruptionState.SPEAKING and self._user_speaking():
                # Native provisional pause flips the agent back to listening while
                # the user talks; the episode is owned by the user_state handler,
                # but cover a missed ordering here.
                self._open_episode()
            elif self.state == InterruptionState.SPEAKING:
                # A completed response must not leave SPEAKING behind to classify
                # the next ordinary user turn as an interruption.
                self._clear_episode(reason="assistant_finished_user_silent")
            return

    def _on_user_state(self, event) -> None:
        new_state = getattr(event, "new_state", None)
        if new_state == "speaking":
            if self._agent_speaking() or self.state == InterruptionState.SPEAKING:
                self._open_episode()
            elif self.state == InterruptionState.PROVISIONAL_INTERRUPTION:
                self._open_episode()
            else:
                # Ordinary user turn while the agent is idle; no episode.
                self.state = InterruptionState.IDLE
                self._episode = None
            return
        if new_state in ("listening", "away"):
            if self.state == InterruptionState.PROVISIONAL_INTERRUPTION and self._episode and self._episode.user_eos_at is None:
                self._episode.user_eos_at = time.time()
                logger.info("INTERRUPTION waiting_for_transcription episode=%d", self._episode.id)

    def _on_transcribed(self, event) -> None:
        text = getattr(event, "transcript", "") or ""
        is_final = bool(getattr(event, "is_final", False))
        if not is_final:
            if self._confirm_final_only:
                return
            if self.state == InterruptionState.PROVISIONAL_INTERRUPTION and text.strip() and self._episode:
                self._confirm(self._episode, len(text), source="interim")
            return
        if not text.strip():
            return
        if self.state == InterruptionState.PROVISIONAL_INTERRUPTION and self._episode:
            self._confirm(self._episode, len(text), source="final")
        elif self.state == InterruptionState.FALSE_INTERRUPTION and self._episode:
            # The native false-interruption timer may resume playout before a
            # CPU-backed streaming STT flush completes. A later non-empty FINAL
            # still belongs to this VAD episode and must confirm it.
            self._confirm(self._episode, len(text), source="late_final")
        elif self._agent_speaking():
            # VAD onset was missed but the agent is speaking: confirm directly so
            # the final still cancels the speech exactly once via the safety net.
            self._confirm(self._open_episode(), len(text), source="final")

    def _on_false_interruption(self, event) -> None:
        if self.state != InterruptionState.PROVISIONAL_INTERRUPTION or not self._episode:
            return
        self._episode.resumes += 1
        if bool(getattr(event, "resumed", False)):
            logger.info("INTERRUPTION speech_resumed episode=%d", self._episode.id)
        else:
            logger.debug("INTERRUPTION false_quiet episode=%d (speech already finished)", self._episode.id)
        if self._episode.resumes > 1:
            logger.warning("INTERRUPTION resume_repeated episode=%d count=%d", self._episode.id, self._episode.resumes)
        # Deliberately stay PROVISIONAL: a slow STT final may still confirm this
        # episode. FALSE is declared only by the settle bound or transcription
        # timeout, never by the resume itself.

    def _on_transcription_timeout(self, event) -> None:
        if self.state == InterruptionState.PROVISIONAL_INTERRUPTION and self._episode:
            self._declare_false(self._episode, reason="transcription_timeout")

    # -- episode machinery ---------------------------------------------------

    def _agent_speaking(self) -> bool:
        try:
            return self._session.agent_state == "speaking"
        except Exception:
            return False

    def _user_speaking(self) -> bool:
        try:
            return self._session.user_state == "speaking"
        except Exception:
            return False

    def _open_episode(self) -> InterruptionEpisode:
        if self.state == InterruptionState.PROVISIONAL_INTERRUPTION and self._episode:
            self._arm_settle(self._episode)
            return self._episode
        self._episode_seq += 1
        speech = None
        try:
            speech = self._session.current_speech
        except Exception:
            speech = None
        self._episode = InterruptionEpisode(id=self._episode_seq, speech_id=getattr(speech, "id", None))
        self.state = InterruptionState.PROVISIONAL_INTERRUPTION
        logger.info("INTERRUPTION candidate episode=%d", self._episode.id)
        self._arm_settle(self._episode)
        return self._episode

    def _confirm(self, episode: InterruptionEpisode, chars: int, *, source: str) -> None:
        episode.final_chars = chars
        episode.outcome = f"confirmed_{source}"
        self.state = InterruptionState.CONFIRMED_INTERRUPTION
        self._clear_settle()
        self._clear_verify()
        logger.info("INTERRUPTION confirmed episode=%d chars=%d", episode.id, chars)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        def verify() -> None:
            self._verify_handle = None
            if self._episode is episode and self.state == InterruptionState.CONFIRMED_INTERRUPTION:
                asyncio.ensure_future(self._verify_cancelled(episode))
        self._verify_handle = loop.call_later(_CANCEL_VERIFY_DELAY_SECONDS, verify)

    async def _verify_cancelled(self, episode: InterruptionEpisode) -> None:
        """Safety bound: guarantee the confirmed speech is cancelled exactly once."""
        if self._episode is not episode or self.state != InterruptionState.CONFIRMED_INTERRUPTION:
            logger.info("INTERRUPTION verify_skipped episode=%d reason=stale_episode", episode.id)
            return
        if episode.speech_id is None:
            logger.info("INTERRUPTION verify_skipped episode=%d reason=missing_speech", episode.id)
            return
        handle = None
        try:
            handle = self._session.current_speech
        except Exception:
            handle = None
        handle_id = getattr(handle, "id", None)
        try:
            interrupted = bool(handle.interrupted) if handle is not None else True
        except Exception:
            interrupted = True
        try:
            done = bool(handle.done()) if handle is not None else True
        except Exception:
            done = True
        if handle is None or done or interrupted or handle_id != episode.speech_id:
            logger.info("INTERRUPTION speech_cancelled episode=%d", episode.id)
            return
        try:
            await self._session.interrupt()
        except RuntimeError as exc:
            logger.warning("INTERRUPTION speech_cancel_forced_failed episode=%d error=%s", episode.id, exc)
            return
        except Exception:
            logger.exception("INTERRUPTION speech_cancel_forced_failed episode=%d", episode.id)
            return
        logger.info("INTERRUPTION speech_cancelled episode=%d forced=true", episode.id)

    def _declare_false(self, episode: InterruptionEpisode, *, reason: str) -> None:
        episode.outcome = f"false_{reason}"
        self.state = InterruptionState.FALSE_INTERRUPTION
        self._clear_settle()
        self._clear_verify()
        logger.info("INTERRUPTION false episode=%d reason=%s", episode.id, reason)

    def _clear_episode(self, *, reason: str) -> None:
        if self._episode is not None:
            logger.info("INTERRUPTION cleared episode=%d reason=%s", self._episode.id, reason)
        self._clear_settle()
        self._clear_verify()
        self._episode = None
        self.state = InterruptionState.IDLE

    def _arm_settle(self, episode: InterruptionEpisode) -> None:
        self._clear_settle()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        def _on_settle() -> None:
            self._settle_handle = None
            if self.state == InterruptionState.PROVISIONAL_INTERRUPTION and self._episode is episode:
                logger.info("INTERRUPTION confirmation_timeout episode=%d settle=%.1fs", episode.id, self._settle_seconds)
                self._declare_false(episode, reason="settle_timeout")
        self._settle_handle = loop.call_later(self._settle_seconds, _on_settle)

    def _clear_settle(self) -> None:
        if self._settle_handle is not None:
            self._settle_handle.cancel()
            self._settle_handle = None

    def _clear_verify(self) -> None:
        if self._verify_handle is not None:
            self._verify_handle.cancel()
            self._verify_handle = None


def install_interruption_tracker(session, *, settle_seconds: float = 20.0, confirm_final_only: bool = True) -> InterruptionTracker:
    """Attach an InterruptionTracker to a live AgentSession and return it."""
    return InterruptionTracker(session, settle_seconds=settle_seconds, confirm_final_only=confirm_final_only)
