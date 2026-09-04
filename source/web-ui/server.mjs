import crypto from "crypto";
import express from "express";
import { AccessToken } from "livekit-server-sdk";
import { RoomAgentDispatch, RoomConfiguration } from "@livekit/protocol";

const app = express();
const apiKey = process.env.LIVEKIT_API_KEY;
const apiSecret = process.env.LIVEKIT_API_SECRET;
const uiHost = process.env.PUBLIC_UI_HOST;
const rtcHost = process.env.PUBLIC_RTC_HOST;
const turnHost = process.env.PUBLIC_TURN_HOST;
const agentName = "llm-voice";

app.get("/health", (_req, res) => res.json({ status: "ok", service: "llm-web-ui", ui: uiHost, rtc: rtcHost, turn: turnHost, agent: agentName }));
app.get("/token", async (_req, res) => {
  try {
    const identity = `voice-${crypto.randomUUID()}`;
    const room = `voice-${crypto.randomUUID()}`;
    const token = new AccessToken(apiKey, apiSecret, { identity, ttl: "1h" });
    token.addGrant({ roomJoin: true, room, canPublish: true, canSubscribe: true });
    token.roomConfig = new RoomConfiguration({ agents: [new RoomAgentDispatch({ agentName })] });
    res.json({ token: await token.toJwt(), room, identity, agent: agentName, url: `wss://${rtcHost}` });
  } catch (error) {
    console.error("Token generation error:", error);
    res.status(500).json({ error: "Token generation failed" });
  }
});
app.use(express.static("public"));
app.listen(8080, "0.0.0.0", () => console.log(`VoxBigBrain UI listening on http://0.0.0.0:8080; RTC wss://${rtcHost}; TURN ${turnHost}`));
