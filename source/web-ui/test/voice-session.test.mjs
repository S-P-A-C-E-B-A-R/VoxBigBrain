import assert from "node:assert/strict";
import test from "node:test";
import { VoiceSessionRecovery } from "../public/voice-session.js";

function eventTarget() {
  const handlers = new Map();
  return {
    addEventListener(type, handler) { handlers.set(type, handler); },
    removeEventListener(type) { handlers.delete(type); },
    emit(type) { handlers.get(type)?.(); },
  };
}

function setup({ active = true, visible = "visible", wakeLock = true } = {}) {
  const documentRef = { ...eventTarget(), visibilityState: visible };
  const windowRef = eventTarget();
  const sentinel = { ...eventTarget(), releases: 0, async release() { this.releases += 1; } };
  const navigatorRef = { wakeLock: wakeLock ? { calls: 0, async request(type) { this.calls += 1; assert.equal(type, "screen"); return sentinel; } } : undefined };
  const statuses = [], logs = [], recoveries = [];
  const recovery = new VoiceSessionRecovery({ documentRef, windowRef, navigatorRef, isActive: () => active, setStatus: (value) => statuses.push(value), log: (value) => logs.push(value), recoverMicrophone: async (source) => recoveries.push(source) });
  return { documentRef, windowRef, sentinel, navigatorRef, statuses, logs, recoveries, recovery };
}

test("requests and releases Wake Lock only for active session", async () => {
  const { recovery, navigatorRef, sentinel } = setup();
  recovery.start();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(navigatorRef.wakeLock.calls, 1);
  await recovery.stop();
  assert.equal(sentinel.releases, 1);
});

test("does not request Wake Lock while hidden", async () => {
  const { recovery, navigatorRef } = setup({ visible: "hidden" });
  recovery.start();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(navigatorRef.wakeLock.calls, 0);
});

test("foreground visibility restores microphone and Wake Lock", async () => {
  const { recovery, documentRef, navigatorRef, recoveries } = setup();
  recovery.start();
  await new Promise((resolve) => setImmediate(resolve));
  documentRef.visibilityState = "hidden";
  documentRef.emit("visibilitychange");
  documentRef.visibilityState = "visible";
  documentRef.emit("visibilitychange");
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(recoveries, ["visibilitychange"]);
  assert.equal(navigatorRef.wakeLock.calls, 1);
});

test("serializes duplicate resume recovery events", async () => {
  let releaseRecovery;
  const documentRef = { ...eventTarget(), visibilityState: "visible" };
  const windowRef = eventTarget();
  const calls = [];
  const recovery = new VoiceSessionRecovery({ documentRef, windowRef, navigatorRef: {}, isActive: () => true, setStatus: () => {}, log: () => {}, recoverMicrophone: async (source) => { calls.push(source); await new Promise((resolve) => { releaseRecovery = resolve; }); } });
  recovery.start();
  const first = recovery.restore("pageshow");
  const second = recovery.restore("resume");
  assert.strictEqual(first, second);
  await new Promise((resolve) => setImmediate(resolve));
  releaseRecovery();
  await first;
  assert.deepEqual(calls, ["pageshow"]);
});
