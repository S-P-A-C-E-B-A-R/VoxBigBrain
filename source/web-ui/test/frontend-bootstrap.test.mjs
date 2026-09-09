import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

const root = new URL("../", import.meta.url);
const source = await readFile(new URL("public/app.js", root), "utf8");
const html = await readFile(new URL("public/index.html", root), "utf8");

class Element {
  constructor(id) { this.id = id; this.dataset = {}; this.style = {}; this.listeners = new Map(); this.classList = { add() {}, remove() {}, toggle() {} }; }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  async dispatch(type) { await this.listeners.get(type)?.(); }
  setAttribute() {}
  querySelector() { return null; }
}

async function bootstrap({ localStorage, sessionResponse, savedChatsUnavailable = false } = {}) {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  const elements = new Map(ids.map((id) => [id, new Element(id)]));
  const redirects = [];
  elements.get("status").textContent = "Initializing...";
  const document = { getElementById: (id) => elements.get(id) || null, addEventListener() {} };
  const window = { LivekitClient: { Room: class {}, RoomEvent: {}, Track: {} }, addEventListener() {}, location: { assign(url) { redirects.push(url); } } };
  const contextValues = { window, document, console, Date, Map, Set, Math, Number, Object, Boolean, JSON, Promise, setTimeout, clearTimeout, setInterval() { return 0; }, clearInterval() {}, fetch: async (url) => {
    if (url === "/api/session") return sessionResponse || { ok:true, status:200, json: async () => ({ username:"alice", csrfToken:"token" }) };
    if (savedChatsUnavailable) throw new Error("Saved Chats API unavailable");
    return { ok:true, status:200, json: async () => ({ conversations:[] }) };
  } };
  if (localStorage !== undefined) contextValues.localStorage = localStorage;
  const context = vm.createContext(contextValues);
  new vm.Script(source).runInContext(context);
  await new Promise((resolve) => setImmediate(resolve));
  return { elements, redirects };
}

test("bootstraps from current HTML without a Save Chat control", async () => {
  const { elements } = await bootstrap({ localStorage: { getItem() { return null; }, setItem() {} } });
  assert.equal(elements.get("status").textContent, "Ready");
  assert.equal(elements.get("sessionToggle").disabled, false);
  assert.equal(elements.has("saveChat"), false);
});

test("malformed or unavailable persisted volume state cannot block bootstrap", async () => {
  const storages = [
    undefined,
    { getItem() { throw new DOMException("Blocked", "SecurityError"); }, setItem() { throw new DOMException("Blocked", "SecurityError"); } },
    ...["", "legacy", "NaN", "Infinity", "-1", "1.1", "999", "0.5"].map((value) => ({ getItem() { return value; }, setItem() {} })),
  ];
  for (const localStorage of storages) {
    const { elements } = await bootstrap({ localStorage });
    assert.equal(elements.get("status").textContent, "Ready");
    assert.equal(elements.get("sessionToggle").disabled, false);
  }
});

test("stale authenticated session redirects to login", async () => {
  const { redirects } = await bootstrap({ sessionResponse: { ok:false, status:401, json: async () => ({ error:"Authentication required" }) } });
  assert.deepEqual(redirects, ["/login", "/login"]);
});

test("Saved Chats API failure remains isolated after bootstrap", async () => {
  const { elements } = await bootstrap({ savedChatsUnavailable:true });
  elements.get("savedPanel").open = true;
  await elements.get("savedPanel").dispatch("toggle");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(elements.get("status").textContent, "Ready");
  assert.equal(elements.get("savedList").textContent, "Could not refresh saved chats.");
});
