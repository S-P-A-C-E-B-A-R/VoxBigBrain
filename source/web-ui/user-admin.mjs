import readline from "readline/promises";
import { stdin as input, stdout as output } from "process";
import { hash, Algorithm } from "@node-rs/argon2";
import { createDatabase, newId, timestamp } from "./db.mjs";

const usernamePattern = /^[a-zA-Z0-9_.-]{3,32}$/;
const [command, username] = process.argv.slice(2);
const databasePath = process.env.DATABASE_PATH || "/data/voxbigbrain.db";
const db = createDatabase(databasePath);

if (command === "list") {
  for (const user of db.prepare("SELECT username, disabled, created_at FROM users ORDER BY username").all()) console.log(`${user.username}\t${user.disabled ? "disabled" : "active"}\t${new Date(user.created_at).toISOString()}`);
} else if (command === "add") {
  if (!usernamePattern.test(username || "")) throw new Error("Username must be 3-32 characters: letters, numbers, dot, underscore, or hyphen.");
  const prompt = readline.createInterface({ input, output, terminal: true });
  const password = await prompt.question("Password (minimum 12 characters): ", { hideEchoBack: true });
  prompt.close();
  if (password.length < 12 || password.length > 256) throw new Error("Password must be 12-256 characters.");
  const time = timestamp();
  db.prepare("INSERT INTO users (id, username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?)").run(newId(), username, await hash(password, { algorithm: Algorithm.Argon2id }), time, time);
  console.log(`Created user ${username}.`);
} else if (command === "disable") {
  if (!usernamePattern.test(username || "")) throw new Error("Provide a valid username.");
  const result = db.prepare("UPDATE users SET disabled = 1, updated_at = ? WHERE username = ?").run(timestamp(), username);
  if (!result.changes) throw new Error("User not found.");
  db.prepare("UPDATE sessions SET revoked_at = ? WHERE user_id = (SELECT id FROM users WHERE username = ?)").run(timestamp(), username);
  console.log(`Disabled user ${username} and revoked active sessions.`);
} else {
  throw new Error("Usage: npm run user:add -- <username> | user:list | user:disable -- <username>");
}
