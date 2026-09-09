import crypto from "crypto";
import Database from "better-sqlite3";

const now = () => Date.now();

export function createDatabase(filename) {
  const db = new Database(filename);
  db.pragma("journal_mode = WAL");
  db.pragma("foreign_keys = ON");
  db.exec("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)");
  const migrations = [
    `CREATE TABLE users (
      id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
      disabled INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
    );
    CREATE TABLE sessions (
      id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, csrf_hash TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
      created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL, revoked_at INTEGER
    );
    CREATE INDEX sessions_user_id ON sessions(user_id);
    CREATE TABLE conversations (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
    );
    CREATE INDEX conversations_user_updated ON conversations(user_id, updated_at DESC);
    CREATE TABLE messages (
      id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
      source_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user', 'assistant')), content TEXT NOT NULL,
      sequence INTEGER NOT NULL, created_at INTEGER NOT NULL, UNIQUE(conversation_id, source_id), UNIQUE(conversation_id, sequence)
    );
    CREATE TABLE voice_sessions (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), conversation_id TEXT REFERENCES conversations(id),
      room TEXT NOT NULL UNIQUE, runtime_identity TEXT NOT NULL UNIQUE,
      created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER
    );
    CREATE INDEX voice_sessions_user_active ON voice_sessions(user_id, expires_at);`,
  ];
  for (let i = 0; i < migrations.length; i += 1) {
    if (!db.prepare("SELECT 1 FROM schema_migrations WHERE version = ?").get(i + 1)) {
      const transaction = db.transaction(() => {
        db.exec(migrations[i]);
        db.prepare("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)").run(i + 1, now());
      });
      transaction();
    }
  }
  return db;
}

export const hashToken = (value, secret = "") => crypto.createHmac("sha256", secret).update(value).digest("hex");
export const newId = () => crypto.randomUUID();
export const timestamp = now;

export function appendMessages(db, conversationId, messages) {
  const insert = db.prepare("INSERT OR IGNORE INTO messages (id, conversation_id, source_id, role, content, sequence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)");
  const nextSequence = db.prepare("SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM messages WHERE conversation_id = ?");
  const touch = db.prepare("UPDATE conversations SET updated_at = ? WHERE id = ?");
  const write = db.transaction((items) => {
    for (const item of items) {
      if (!item || !["user", "assistant"].includes(item.role) || typeof item.content !== "string" || !item.content.trim() || typeof item.sourceId !== "string") continue;
      insert.run(newId(), conversationId, item.sourceId, item.role, item.content.trim().slice(0, 20000), nextSequence.get(conversationId).value, item.createdAt || now());
    }
    touch.run(now(), conversationId);
  });
  write(messages);
}
