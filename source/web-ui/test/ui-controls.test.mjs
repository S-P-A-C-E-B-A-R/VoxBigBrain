import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);
const [html, app, css] = await Promise.all([
  readFile(new URL("public/index.html", root), "utf8"),
  readFile(new URL("public/app.js", root), "utf8"),
  readFile(new URL("public/styles.css", root), "utf8"),
]);

test("uses one accessible stateful session control", () => {
  assert.match(html, /id="sessionToggle"[^>]*aria-label="Connect"[^>]*title="Connect"/);
  assert.doesNotMatch(html, /id="connect"|id="disconnect"/);
  assert.match(app, /function setSessionToggle\(state\)/);
  assert.match(app, /state === "connecting"/);
  assert.match(app, /state === "connected"/);
  assert.match(app, /sessionToggle\.disabled = busy/);
});

test("uses accessible compact icon controls and persisted sound volume", () => {
  for (const id of ["mute", "newChat", "saveChat"]) assert.match(html, new RegExp(`id="${id}"[^>]*class="icon-button"[^>]*aria-label="[^"]+"[^>]*title="[^"]+"`));
  assert.match(html, /id="uiSoundVolume"[^>]*type="range"[^>]*aria-label="UI sound volume"/);
  assert.match(app, /UI_SOUND_VOLUME_KEY = "voxbigbrain\.uiSoundVolume"/);
  assert.match(app, /localStorage\.setItem\(UI_SOUND_VOLUME_KEY/);
  assert.match(app, /Math\.min\(volume \* uiSoundVolume, \.18\)/);
  assert.match(css, /\.icon-button \{ width:44px; min-width:44px; height:44px/);
});

test("renders mute state through the icon-only helper", () => {
  assert.match(app, /function updateMuteButton\(\)/);
  assert.match(app, /muted \? "Unmute microphone" : "Mute microphone"/);
  assert.doesNotMatch(app, /muteButton\.(textContent|innerText|replaceChildren)/);
});

test("uses the octave-raised thinking pattern with existing timing and volume scaling", () => {
  assert.match(app, /\[\[660, 0\], \[1240, \.135\], \[940, \.27\]\]/);
  assert.match(app, /tone\(frequency, \.1, delay, \.8\)/);
  assert.match(app, /}, 1700\)/);
  assert.match(app, /}, 120\)/);
});
