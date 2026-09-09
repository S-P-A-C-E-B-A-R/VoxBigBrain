import asyncio
import logging
import time

import aiohttp

from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, mcp, room_io, stt
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.plugins import openai, silero

from .config import config
from .interruption import install_interruption_tracker
from .prompts import current_time_context, instructions
from .telemetry import install_tool_telemetry
from .whisper_streaming import FasterWhisperLiveSTT, WhisperLiveOptions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
server = AgentServer()


def make_kagi_toolset():
    return mcp.MCPToolset(
        id="kagi",
        mcp_server=mcp.MCPServerHTTP(
            "https://mcp.kagi.com/mcp",
            headers={"Authorization": f"Bearer {config.kagi_api_key}"},
            allowed_tools=["kagi_search_fetch"],
        ),
    )


class VoiceAssistant(Agent):
    def __init__(self, chat_ctx: ChatContext):
        super().__init__(instructions=instructions(current_time_context(config.timezone)), chat_ctx=chat_ctx, tools=[make_kagi_toolset()])


def log_stt_metrics(metrics) -> None:
    logger.info(
        "STT segment completed audio_duration=%.2fs request_duration=%.2fs",
        metrics.audio_duration,
        metrics.duration,
    )


def make_stt(speech_vad):
    if config.whisper_streaming_enabled:
        return FasterWhisperLiveSTT(
            options=WhisperLiveOptions(config.whisper_ws_url, config.whisper_model, config.whisper_language, config.whisper_temperature, config.whisper_vad_filter, finalization_seconds=config.whisper_ws_finalization_seconds),
            speech_vad=speech_vad,
        )
    whisper_stt = openai.STT(model=config.whisper_model, base_url=config.whisper_base_url, api_key="not-needed", language=config.whisper_language)
    return stt.StreamAdapter(stt=whisper_stt, vad=speech_vad)


def turn_handling_options():
    return {
        "interruption": {
            "mode": "vad",
            "min_duration": config.interruption_min_duration,
            "min_words": config.interruption_min_words,
            "false_interruption_timeout": config.false_interruption_timeout,
            "resume_false_interruption": True,
        },
        # LiveKit Agents 1.7.1 defaults this to True. Keep LLM generation behind
        # completed endpointing while CPU Whisper finalization is still pending.
        "preemptive_generation": {"enabled": False},
    }


def log_turn_timeline(event: str, **fields) -> None:
    details = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
    logger.info("TURN_TIMELINE t=%.6f event=%s%s", time.monotonic(), event, f" {details}" if details else "")


@server.rtc_session(agent_name="llm-voice")
async def voice_session(ctx: agents.JobContext):
    time_context = current_time_context(config.timezone)
    logger.info("New voice session room=%s date=%s time=%s timezone=%s", ctx.room.name, time_context["date"], time_context["time"], time_context["timezone"])
    logger.info("Services whisper=%s qwen=%s kokoro=%s mcp=kagi enabled", config.whisper_base_url, config.qwen_base_url, config.kokoro_base_url)
    # The streaming adapter and AgentSession share Silero settings. The adapter
    # forwards only Silero-approved frames; the session drives turn and barge-in state.
    vad = silero.VAD.load(
        activation_threshold=config.vad_activation_threshold,
        min_speech_duration=config.vad_min_speech_duration,
        min_silence_duration=config.vad_min_silence_duration,
        prefix_padding_duration=config.vad_prefix_padding_duration,
    )
    active_stt = make_stt(vad)
    active_stt.on("metrics_collected", log_stt_metrics)
    participant = await ctx.wait_for_participant()
    identity = participant.identity
    headers = {"Authorization": f"Bearer {config.internal_agent_secret}"}
    async with aiohttp.ClientSession(headers=headers) as http:
        async with http.get(f"{config.web_internal_url}/internal/voice-sessions/{identity}") as response:
            if response.status != 200:
                raise RuntimeError("Voice session authorization was not found")
            restored = await response.json()
    chat_ctx = ChatContext.empty()
    for message in restored["messages"]:
        chat_ctx.add_message(role=message["role"], content=message["content"], id=message["id"], created_at=message["created_at"] / 1000)
    session = AgentSession(
        stt=active_stt,
        llm=openai.LLM(model=config.qwen_model, base_url=config.qwen_base_url, api_key=config.qwen_api_key),
        tts=openai.TTS(model=config.kokoro_model, voice=config.kokoro_voice, base_url=config.kokoro_base_url, api_key="not-needed", response_format="wav"),
        vad=vad,
        turn_handling=turn_handling_options(),
    )
    # Supervisory two-phase interruption confirmation. The framework keeps owning
    # VAD pause/resume/cancel; the tracker only mirrors states, confirms on FINAL
    # transcripts, and emits compact INTERRUPTION diagnostics.
    tracker = install_interruption_tracker(session, settle_seconds=config.interruption_transcription_settle_seconds, confirm_final_only=config.interruption_confirm_final_only)
    @session.on("user_state_changed")
    def log_user_state(event) -> None:
        if event.new_state == "speaking":
            log_turn_timeline("vad_speech_start", old=event.old_state)
        elif event.old_state == "speaking":
            log_turn_timeline("vad_speech_end", new=event.new_state)
        else:
            log_turn_timeline("user_state", new=event.new_state, old=event.old_state)

    @session.on("user_input_transcribed")
    def log_transcription(event) -> None:
        log_turn_timeline(
            "stt_final" if event.is_final else "stt_interim",
            chars=len(event.transcript or ""),
            segment=event.item_id or "-",
        )

    @session.on("agent_state_changed")
    def log_agent_state(event) -> None:
        log_turn_timeline("agent_state", new=event.new_state, old=event.old_state)

    @session.on("speech_created")
    def log_speech_created(event) -> None:
        log_turn_timeline(
            "llm_generation_created" if event.source == "generate_reply" else "speech_created",
            source=event.source,
            speech=event.speech_handle.id,
            user_initiated=event.user_initiated,
        )

    @session.on("conversation_item_added")
    def persist_final_message(event) -> None:
        item = event.item
        if isinstance(item, ChatMessage):
            log_turn_timeline(
                "user_turn_committed" if item.role == "user" else "assistant_output_created",
                chars=len(item.text_content or ""),
                item=item.id,
                interrupted=item.interrupted,
                role=item.role,
            )
        if not isinstance(item, ChatMessage) or not tracker.should_persist(item):
            return
        content = item.text_content
        async def persist() -> None:
            try:
                async with aiohttp.ClientSession(headers=headers) as http:
                    async with http.post(
                        f"{config.web_internal_url}/internal/voice-sessions/{identity}/messages",
                        json={"sourceId": item.id, "role": item.role, "content": content, "createdAt": int(item.created_at * 1000)},
                    ) as response:
                        if response.status != 204:
                            logger.warning("Conversation persistence event rejected status=%s", response.status)
            except Exception:
                logger.exception("Conversation persistence event failed")
        asyncio.create_task(persist())
    install_tool_telemetry(session, ctx.room, config.timezone)
    await session.start(room=ctx.room, agent=VoiceAssistant(chat_ctx), room_options=room_io.RoomOptions(audio_input=True, audio_output=True, text_input=True, text_output=True))


if __name__ == "__main__":
    logger.info(
        "Starting llm-voice; Kagi MCP enabled; timezone=%s; "
        "vad activation_threshold=%.2f min_speech_duration=%.2fs "
        "min_silence_duration=%.2fs prefix_padding_duration=%.2fs; "
        "interruption min_duration=%.2fs min_words=%d false_timeout=%.2fs settle=%.1fs confirm_final_only=%s; preemptive_generation=false; whisper streaming=%s temperature=%.1f vad_filter=%s",
        config.timezone,
        config.vad_activation_threshold,
        config.vad_min_speech_duration,
        config.vad_min_silence_duration,
        config.vad_prefix_padding_duration,
        config.interruption_min_duration,
        config.interruption_min_words,
        config.false_interruption_timeout,
        config.interruption_transcription_settle_seconds,
        config.interruption_confirm_final_only,
        config.whisper_streaming_enabled,
        config.whisper_temperature,
        config.whisper_vad_filter,
    )
    agents.cli.run_app(server)
