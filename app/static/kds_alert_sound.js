/**
 * نغمات تنبيه KDS — Web Audio (لا تحتاج ملفات خارجية).
 * window.KdsAlertSound.play(profile, volumePercent)
 */
(function (global) {
  "use strict";

  var ctx = null;
  var unlocked = false;

  var PROFILES = {
    beep: { label: "نغمة قصيرة (افتراضي)" },
    chime: { label: "جرس لطيف" },
    bell: { label: "جرس مطبخ" },
    kitchen: { label: "ثلاث نغمات" },
    alert: { label: "تنبيه قوي" },
  };

  function normVolume(pct) {
    var v = Number(pct);
    if (!isFinite(v)) v = 80;
    return Math.max(0.05, Math.min(1, v / 100));
  }

  function getCtx() {
    if (ctx) return ctx;
    var Ctx = global.AudioContext || global.webkitAudioContext;
    if (!Ctx) return null;
    ctx = new Ctx();
    return ctx;
  }

  function tone(ac, freq, start, dur, vol, type) {
    var osc = ac.createOscillator();
    var gain = ac.createGain();
    osc.type = type || "sine";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(vol, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + dur);
    osc.connect(gain);
    gain.connect(ac.destination);
    osc.start(start);
    osc.stop(start + dur + 0.05);
  }

  function playProfile(profile, volumePct) {
    var ac = getCtx();
    if (!ac) return Promise.reject(new Error("no-audio"));
    if (ac.state === "suspended") {
      return ac.resume().then(function () {
        return playProfile(profile, volumePct);
      });
    }
    var vol = normVolume(volumePct);
    var t0 = ac.currentTime;
    var p = (profile || "beep").toLowerCase();

    if (p === "chime") {
      tone(ac, 660, t0, 0.18, vol);
      tone(ac, 880, t0 + 0.14, 0.22, vol);
      tone(ac, 1100, t0 + 0.32, 0.28, vol);
    } else if (p === "bell") {
      tone(ac, 830, t0, 0.55, vol);
      tone(ac, 1245, t0, 0.45, vol * 0.35, "triangle");
    } else if (p === "kitchen") {
      tone(ac, 880, t0, 0.12, vol);
      tone(ac, 880, t0 + 0.2, 0.12, vol);
      tone(ac, 1100, t0 + 0.4, 0.2, vol);
    } else if (p === "alert") {
      tone(ac, 520, t0, 0.15, vol, "square");
      tone(ac, 780, t0 + 0.18, 0.15, vol, "square");
      tone(ac, 520, t0 + 0.36, 0.15, vol, "square");
    } else {
      tone(ac, 880, t0, 0.28, vol);
    }

    return Promise.resolve();
  }

  function unlock() {
    var ac = getCtx();
    if (!ac) return Promise.resolve(false);
    return ac.resume().then(function () {
      unlocked = ac.state === "running";
      return unlocked;
    }).catch(function () {
      return false;
    });
  }

  function isUnlocked() {
    return unlocked && ctx && ctx.state === "running";
  }

  global.KdsAlertSound = {
    profiles: PROFILES,
    profileKeys: Object.keys(PROFILES),
    play: playProfile,
    unlock: unlock,
    isUnlocked: isUnlocked,
    normVolume: normVolume,
  };
})(typeof window !== "undefined" ? window : this);
