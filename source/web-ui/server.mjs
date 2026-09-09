import crypto from "crypto";
import path from "path";
import { fileURLToPath } from "url";
import express from "express";
import { hash, verify, Algorithm } from "@node-rs/argon2";
import { AccessToken, RoomServiceClient } from "livekit-server-sdk";
import { RoomAgentDispatch, RoomConfiguration } from "@livekit/protocol";
import { appendMessages, createDatabase, hashToken, newId, timestamp } from "./db.mjs";

const root = path.dirname(fileURLToPath(import.meta.url));
const agentName = "llm-voice";
const usernamePattern = /^[a-zA-Z0-9_.-]{3,32}$/;

function required(name, env) {
  const value = env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function configFromEnv(env = process.env) {
  if (env.AUTH_ENABLED !== undefined && env.AUTH_ENABLED !== "true") throw new Error("AUTH_ENABLED must be true; anonymous operation is not supported.");
  const sessionSecret = required("SESSION_SECRET", env);
  if (sessionSecret.length < 32) throw new Error("SESSION_SECRET must be at least 32 characters.");
  return {
    apiKey: required("LIVEKIT_API_KEY", env), apiSecret: required("LIVEKIT_API_SECRET", env), rtcHost: required("PUBLIC_RTC_HOST", env), livekitInternalUrl: env.LIVEKIT_INTERNAL_URL || "http://livekit:7880",
    sessionSecret, internalAgentSecret: required("INTERNAL_AGENT_SECRET", env), databasePath: env.DATABASE_PATH || "/data/voxbigbrain.db",
    sessionTtlMs: Number(env.SESSION_TTL_HOURS || 168) * 3600000,
    contextMaxMessages: Number(env.CHAT_CONTEXT_MAX_MESSAGES || 50), loginAttempts: Number(env.LOGIN_RATE_LIMIT_ATTEMPTS || 10),
    loginWindowMs: Number(env.LOGIN_RATE_LIMIT_WINDOW_MINUTES || 15) * 60000, maxVoiceSessions: Number(env.MAX_ACTIVE_VOICE_SESSIONS_PER_USER || 2),
    voiceRateLimit: Number(env.VOICE_SESSION_RATE_LIMIT_PER_MINUTE || 10), voiceSessionTtlMs: Number(env.VOICE_SESSION_TTL_MINUTES || 10) * 60000,
    secureCookies: env.NODE_ENV === "production",
  };
}

function rateLimiter() {
  const entries = new Map();
  return (key, limit, windowMs) => {
    const time = Date.now();
    const values = (entries.get(key) || []).filter((value) => value > time - windowMs);
    if (values.length >= limit) return false;
    values.push(time); entries.set(key, values); return true;
  };
}

function cookies(req) {
  return Object.fromEntries((req.headers.cookie || "").split(";").map((value) => value.trim().split(/=(.*)/s)).filter(([key]) => key).map(([key, value]) => [key, decodeURIComponent(value || "")]));
}
function randomToken() { return crypto.randomBytes(32).toString("base64url"); }
function safeEqual(left, right) { return typeof left === "string" && typeof right === "string" && left.length === right.length && crypto.timingSafeEqual(Buffer.from(left), Buffer.from(right)); }
function clientIp(req) { return req.ip || req.socket.remoteAddress || "unknown"; }

export function createApp(overrides = {}) {
  const config = { ...configFromEnv(overrides.env || process.env), ...overrides.config };
  const db = overrides.db || createDatabase(config.databasePath);
  const app = express();
  const allow = rateLimiter();
  const runtimeHistory = new Map();
  const roomService = new RoomServiceClient(config.livekitInternalUrl, config.apiKey, config.apiSecret);
  app.disable("x-powered-by");
  app.set("trust proxy", 1);
  app.use(express.json({ limit: "64kb" }));
  app.use((req, res, next) => {
    res.set({ "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Permissions-Policy": "microphone=(self), camera=(), geolocation=()", "Content-Security-Policy": "default-src 'self'; connect-src 'self' wss:; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'" });
    next();
  });
  app.get("/health", (_req, res) => res.json({ status: "ok" }));

  function setCookie(res, name, value, httpOnly = true, maxAge) {
    res.cookie(name, value, { httpOnly, secure: config.secureCookies, sameSite: "lax", path: "/", maxAge });
  }
  function clearCookie(res, name) { res.clearCookie(name, { httpOnly: name !== "vbb_login_csrf", secure: config.secureCookies, sameSite: "lax", path: "/" }); }
  function requireOrigin(req, res, next) {
    const origin = req.get("origin");
    if (origin && origin !== `${req.protocol}://${req.get("host")}`) return res.status(403).json({ error: "Request rejected" });
    next();
  }
  function session(req) {
    const token = cookies(req).vbb_session;
    if (!token) return null;
    const record = db.prepare("SELECT sessions.*, users.username, users.disabled FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.token_hash = ?").get(hashToken(token, config.sessionSecret));
    if (!record || record.revoked_at || record.disabled || record.expires_at <= timestamp()) return null;
    db.prepare("UPDATE sessions SET last_seen_at = ? WHERE id = ?").run(timestamp(), record.id);
    return record;
  }
  function requireAuth(req, res, next) { req.auth = session(req); if (!req.auth) return res.status(401).json({ error: "Authentication required" }); next(); }
  function requirePageAuth(req, res, next) { req.auth = session(req); if (!req.auth) return res.redirect(303, "/login"); next(); }
  function requireCsrf(req, res, next) {
    if (!safeEqual(hashToken(req.get("x-csrf-token") || "", config.sessionSecret), req.auth.csrf_hash)) return res.status(403).json({ error: "Request rejected" });
    next();
  }
  function issueSession(res, userId) {
    const token = randomToken(), csrf = randomToken(), time = timestamp(), expires = time + config.sessionTtlMs;
    db.prepare("INSERT INTO sessions (id, token_hash, csrf_hash, user_id, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)").run(newId(), hashToken(token, config.sessionSecret), hashToken(csrf, config.sessionSecret), userId, time, expires, time);
    setCookie(res, "vbb_session", token, true, config.sessionTtlMs); return csrf;
  }
  async function revokeVoiceSessions(userId, id = null) {
    const voices = db.prepare(`SELECT id, room, runtime_identity FROM voice_sessions WHERE user_id = ? AND revoked_at IS NULL${id ? " AND id = ?" : ""}`).all(userId, ...(id ? [id] : []));
    if (!voices.length) return;
    db.prepare(`UPDATE voice_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL${id ? " AND id = ?" : ""}`).run(timestamp(), userId, ...(id ? [id] : []));
    for (const voice of voices) {
      runtimeHistory.delete(voice.id);
      try { await roomService.removeParticipant(voice.room, voice.runtime_identity); } catch { /* The room may already be closed. */ }
    }
  }
  function loginCsrf(req, res) {
    const existing = cookies(req).vbb_login_csrf;
    const token = existing || randomToken();
    if (!existing) setCookie(res, "vbb_login_csrf", token, false, 10 * 60000);
    return token;
  }
  app.get("/api/auth/csrf", (req, res) => res.json({ csrfToken: loginCsrf(req, res) }));
  app.post("/api/login", requireOrigin, async (req, res) => {
    const { username = "", password = "" } = req.body || {};
    if (!safeEqual(req.get("x-csrf-token") || "", cookies(req).vbb_login_csrf || "")) return res.status(403).json({ error: "Request rejected" });
    const key = `${clientIp(req)}:${String(username).toLowerCase()}`;
    if (!allow(`login:${key}`, config.loginAttempts, config.loginWindowMs)) return res.status(429).json({ error: "Too many attempts. Try again later." });
    const user = db.prepare("SELECT * FROM users WHERE username = ? COLLATE NOCASE").get(String(username));
    const valid = user && !user.disabled ? await verify(user.password_hash, String(password)) : (await hash(String(password), { algorithm: Algorithm.Argon2id }), false);
    if (!valid) return res.status(401).json({ error: "Invalid username or password" });
    clearCookie(res, "vbb_login_csrf");
    res.json({ username: user.username, csrfToken: issueSession(res, user.id) });
  });
  app.post("/api/logout", requireOrigin, requireAuth, requireCsrf, async (req, res) => {
    db.prepare("UPDATE sessions SET revoked_at = ? WHERE id = ?").run(timestamp(), req.auth.id); await revokeVoiceSessions(req.auth.user_id); clearCookie(res, "vbb_session"); res.status(204).end();
  });
  // Rotate the session CSRF token whenever it is requested; it is never put in a cookie.
  app.get("/api/session", requireAuth, (req, res) => {
    const csrfToken = randomToken(); db.prepare("UPDATE sessions SET csrf_hash = ? WHERE id = ?").run(hashToken(csrfToken, config.sessionSecret), req.auth.id);
    res.json({ username: req.auth.username, csrfToken });
  });

  app.post("/api/voice/session", requireOrigin, requireAuth, requireCsrf, async (req, res) => {
    if (!allow(`voice:${req.auth.user_id}`, config.voiceRateLimit, 60000)) return res.status(429).json({ error: "Too many voice sessions. Try again shortly." });
    const conversationId = typeof req.body?.conversationId === "string" ? req.body.conversationId : null;
    if (conversationId && !db.prepare("SELECT 1 FROM conversations WHERE id = ? AND user_id = ?").get(conversationId, req.auth.user_id)) return res.status(404).json({ error: "Conversation not found" });
    db.prepare("UPDATE voice_sessions SET revoked_at = ? WHERE expires_at <= ?").run(timestamp(), timestamp());
    const active = db.prepare("SELECT COUNT(*) AS count FROM voice_sessions WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?").get(req.auth.user_id, timestamp()).count;
    if (active >= config.maxVoiceSessions) return res.status(429).json({ error: "Maximum active voice sessions reached" });
    const id = newId(), identity = `voice-${newId()}`, room = `voice-${newId()}`, expiresAt = timestamp() + config.voiceSessionTtlMs;
    db.prepare("INSERT INTO voice_sessions (id, user_id, conversation_id, room, runtime_identity, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)").run(id, req.auth.user_id, conversationId, room, identity, timestamp(), expiresAt);
    runtimeHistory.set(id, []);
    const token = new AccessToken(config.apiKey, config.apiSecret, { identity, ttl: Math.max(1, Math.floor((Math.min(req.auth.expires_at, expiresAt) - timestamp()) / 1000)) });
    token.addGrant({ roomJoin: true, room, canPublish: true, canSubscribe: true }); token.roomConfig = new RoomConfiguration({ agents: [new RoomAgentDispatch({ agentName })] });
    res.json({ token: await token.toJwt(), room, identity, voiceSessionId: id, agent: agentName, url: `wss://${config.rtcHost}` });
  });
  app.post("/api/voice/session/:id/end", requireOrigin, requireAuth, requireCsrf, async (req, res) => {
    const voice = db.prepare("SELECT id FROM voice_sessions WHERE id = ? AND user_id = ? AND revoked_at IS NULL").get(req.params.id, req.auth.user_id);
    if (!voice) return res.status(404).json({ error: "Voice session not found" });
    await revokeVoiceSessions(req.auth.user_id, voice.id); res.status(204).end();
  });
  app.get("/api/conversations", requireAuth, (req, res) => res.json({ conversations: db.prepare("SELECT id, title, created_at, updated_at FROM conversations WHERE user_id = ? ORDER BY updated_at DESC").all(req.auth.user_id) }));
  app.get("/api/conversations/:id", requireAuth, (req, res) => {
    const conversation = db.prepare("SELECT id, title, created_at, updated_at FROM conversations WHERE id = ? AND user_id = ?").get(req.params.id, req.auth.user_id);
    if (!conversation) return res.status(404).json({ error: "Conversation not found" });
    res.json({ conversation, messages: db.prepare("SELECT id, role, content, sequence, created_at FROM messages WHERE conversation_id = ? ORDER BY sequence").all(conversation.id) });
  });
  app.patch("/api/conversations/:id", requireOrigin, requireAuth, requireCsrf, (req, res) => {
    const title = typeof req.body?.title === "string" ? req.body.title.trim() : "";
    if (!title || title.length > 120) return res.status(400).json({ error: "Title must be 1-120 characters" });
    const result = db.prepare("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?").run(title, timestamp(), req.params.id, req.auth.user_id);
    if (!result.changes) return res.status(404).json({ error: "Conversation not found" });
    res.json({ id: req.params.id, title });
  });
  app.delete("/api/conversations/:id", requireOrigin, requireAuth, requireCsrf, (req, res) => {
    const remove = db.transaction(() => {
      const conversation = db.prepare("SELECT id FROM conversations WHERE id = ? AND user_id = ?").get(req.params.id, req.auth.user_id);
      if (!conversation) return false;
      // A connected agent keeps this runtime history ephemeral after its saved chat is deleted.
      db.prepare("UPDATE voice_sessions SET conversation_id = NULL WHERE user_id = ? AND conversation_id = ?").run(req.auth.user_id, conversation.id);
      db.prepare("DELETE FROM conversations WHERE id = ? AND user_id = ?").run(conversation.id, req.auth.user_id);
      return true;
    });
    if (!remove()) return res.status(404).json({ error: "Conversation not found" });
    res.status(204).end();
  });
  app.post("/api/conversations/save", requireOrigin, requireAuth, requireCsrf, (req, res) => {
    const voiceSessionId = req.body?.voiceSessionId;
    const voice = db.prepare("SELECT * FROM voice_sessions WHERE id = ? AND user_id = ? AND revoked_at IS NULL AND expires_at > ?").get(voiceSessionId, req.auth.user_id, timestamp());
    if (!voice) return res.status(404).json({ error: "Voice session not found" });
    let conversationId = voice.conversation_id;
    if (!conversationId) {
      const buffered = runtimeHistory.get(voice.id) || [];
      const firstUser = buffered.find((message) => message.role === "user")?.content;
      conversationId = newId();
      db.prepare("INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)").run(conversationId, req.auth.user_id, (firstUser || `Chat ${new Date().toLocaleString()}`).slice(0, 120), timestamp(), timestamp());
      db.prepare("UPDATE voice_sessions SET conversation_id = ? WHERE id = ?").run(conversationId, voice.id);
      appendMessages(db, conversationId, buffered);
    }
    res.json({ conversationId });
  });

  function internalAuth(req, res, next) { if (!safeEqual(req.get("authorization")?.replace(/^Bearer /, "") || "", config.internalAgentSecret)) return res.status(401).end(); next(); }
  app.get("/internal/voice-sessions/:identity", internalAuth, (req, res) => {
    const voice = db.prepare("SELECT * FROM voice_sessions WHERE runtime_identity = ? AND revoked_at IS NULL AND expires_at > ?").get(req.params.identity, timestamp());
    if (!voice) return res.status(404).end();
    const messages = voice.conversation_id ? db.prepare("SELECT id, role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY sequence DESC LIMIT ?").all(voice.conversation_id, config.contextMaxMessages).reverse() : [];
    res.json({ voiceSessionId: voice.id, messages });
  });
  app.post("/internal/voice-sessions/:identity/messages", internalAuth, (req, res) => {
    const voice = db.prepare("SELECT * FROM voice_sessions WHERE runtime_identity = ? AND revoked_at IS NULL AND expires_at > ?").get(req.params.identity, timestamp());
    const message = req.body || {};
    if (!voice || !["user", "assistant"].includes(message.role) || typeof message.content !== "string" || typeof message.sourceId !== "string") return res.status(400).end();
    const item = { role: message.role, content: message.content, sourceId: message.sourceId, createdAt: Number(message.createdAt) || timestamp() };
    const buffer = runtimeHistory.get(voice.id) || []; if (!buffer.some((entry) => entry.sourceId === item.sourceId)) buffer.push(item); runtimeHistory.set(voice.id, buffer);
    if (voice.conversation_id) appendMessages(db, voice.conversation_id, [item]);
    res.status(204).end();
  });

  app.get("/login", (_req, res) => res.sendFile(path.join(root, "public", "login.html")));
  app.get("/login.js", (_req, res) => res.sendFile(path.join(root, "public", "login.js")));
  app.get("/styles.css", (_req, res) => res.sendFile(path.join(root, "public", "styles.css")));
  app.get("/", requirePageAuth, (_req, res) => res.sendFile(path.join(root, "public", "index.html")));
  app.get("/app.js", requireAuth, (_req, res) => res.sendFile(path.join(root, "public", "app.js")));
  app.get("/voice-session.js", requireAuth, (_req, res) => res.sendFile(path.join(root, "public", "voice-session.js")));
  app.get("/livekit-client.js", requireAuth, (_req, res) => res.sendFile(path.join(root, "public", "livekit-client.js")));
  app.use((_req, res) => res.status(404).json({ error: "Not found" }));
  return { app, db };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const { app } = createApp();
  app.listen(8080, "0.0.0.0", () => console.log("VoxBigBrain UI listening on port 8080"));
}
