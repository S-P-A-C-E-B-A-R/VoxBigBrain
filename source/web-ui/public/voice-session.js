export class VoiceSessionRecovery {
  constructor({ documentRef = document, navigatorRef = navigator, windowRef = window, isActive, recoverMicrophone, setStatus, log }) {
    this.document = documentRef;
    this.navigator = navigatorRef;
    this.window = windowRef;
    this.isActive = isActive;
    this.recoverMicrophone = recoverMicrophone;
    this.setStatus = setStatus;
    this.log = log;
    this.wakeLock = null;
    this.recovering = null;
    this.started = false;
    this.onVisibilityChange = () => this.handleVisibilityChange();
    this.onResume = () => this.restore("resume");
    this.onPageShow = () => this.restore("pageshow");
    this.onFreeze = () => this.handleFreeze();
  }

  start() {
    if (this.started) return;
    this.started = true;
    this.document.addEventListener("visibilitychange", this.onVisibilityChange);
    this.window.addEventListener("resume", this.onResume);
    this.window.addEventListener("pageshow", this.onPageShow);
    this.document.addEventListener("freeze", this.onFreeze);
    this.requestWakeLock();
  }

  async stop() {
    this.started = false;
    this.document.removeEventListener("visibilitychange", this.onVisibilityChange);
    this.window.removeEventListener("resume", this.onResume);
    this.window.removeEventListener("pageshow", this.onPageShow);
    this.document.removeEventListener("freeze", this.onFreeze);
    const lock = this.wakeLock;
    this.wakeLock = null;
    if (lock) await lock.release().catch(() => {});
  }

  async requestWakeLock() {
    if (!this.started || !this.isActive() || this.document.visibilityState === "hidden" || this.wakeLock || !this.navigator.wakeLock?.request) return;
    try {
      const lock = await this.navigator.wakeLock.request("screen");
      if (!this.started || !this.isActive()) {
        await lock.release().catch(() => {});
        return;
      }
      this.wakeLock = lock;
      lock.addEventListener?.("release", () => {
        if (this.wakeLock === lock) this.wakeLock = null;
        if (this.started && this.isActive() && this.document.visibilityState !== "hidden") this.requestWakeLock();
      });
    } catch (error) {
      this.log(`Wake Lock unavailable: ${error.name || error}`);
    }
  }

  handleVisibilityChange() {
    if (!this.started || !this.isActive()) return;
    if (this.document.visibilityState === "hidden") {
      // iOS may suspend media capture despite an active screen Wake Lock. Do
      // not claim Listening until the foreground recovery verifies the mic.
      this.setStatus("Reconnecting...");
      return;
    }
    this.restore("visibilitychange");
  }

  handleFreeze() {
    if (this.started && this.isActive()) this.setStatus("Reconnecting...");
  }

  restore(source) {
    if (!this.started || !this.isActive() || this.document.visibilityState === "hidden") return;
    if (this.recovering) return this.recovering;
    this.recovering = (async () => {
      this.setStatus("Restoring microphone...");
      await this.requestWakeLock();
      await this.recoverMicrophone(source);
    })().catch((error) => {
      this.log(`Microphone recovery failed: ${error}`);
      this.setStatus("Disconnected");
      throw error;
    }).finally(() => { this.recovering = null; });
    return this.recovering;
  }
}

if (typeof window !== "undefined") window.VoiceSessionRecovery = VoiceSessionRecovery;
