import asyncio
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)
TOPIC = "llm.tool_status"


def install_tool_telemetry(session, room, timezone_name: str, *, turn_id=lambda: "-", turn_elapsed=lambda: 0.0) -> None:
    tool_names_by_call_id = {}
    timezone = ZoneInfo(timezone_name)

    async def publish(event_type: str, tool_name: str, status: str | None = None) -> None:
        # Browser telemetry deliberately contains lifecycle metadata only, never tool input or output.
        payload = {"type": event_type, "tool": tool_name, "timestamp": datetime.now(timezone).isoformat()}
        if status is not None:
            payload["status"] = status
        try:
            await room.local_participant.send_text(json.dumps(payload), topic=TOPIC)
        except Exception:
            logger.exception("Tool telemetry publish failed")

    @session.on("tool_execution_updated")
    def on_tool_execution_updated(event) -> None:
        update = event.update
        if update.type == "tool_call_started":
            call = update.function_call
            tool_names_by_call_id[call.call_id] = (call.name, time.monotonic())
            logger.info("TURN %s +%.3f event=tool_started tool=%s", turn_id(), turn_elapsed(), call.name)
            asyncio.create_task(publish("tool_started", call.name))
        elif update.type == "tool_call_ended":
            name, started_at = tool_names_by_call_id.pop(update.call_id, ("unknown_tool", time.monotonic()))
            logger.info("TURN %s +%.3f event=tool_finished tool=%s status=%s duration_ms=%d", turn_id(), turn_elapsed(), name, update.status, (time.monotonic() - started_at) * 1000)
            asyncio.create_task(publish("tool_finished", name, str(update.status)))
