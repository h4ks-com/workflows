(() => {
  "use strict";

  const TERMINAL = ["succeeded", "failed", "cancelled"];

  function fmtEta(seconds) {
    if (seconds == null) return "";
    seconds = Math.max(0, Math.round(seconds));
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  function countdowns() {
    document.querySelectorAll("[data-eta]").forEach((el) => {
      let left = Number(el.dataset.eta);
      if (Number.isNaN(left) || el.dataset.etaDone) return;
      left = Math.max(0, left - 1);
      el.dataset.eta = String(left);
      const plain = el.hasAttribute("data-eta-plain");
      el.textContent = left > 0 ? `${plain ? "" : "~"}${fmtEta(left)}` : plain ? "0:00" : "almost done";
    });
  }
  setInterval(countdowns, 1000);

  document.addEventListener("click", (event) => {
    const seg = event.target.closest(".seg button");
    if (seg) {
      seg.parentElement.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === seg));
      const hidden = seg.closest(".seg").nextElementSibling;
      if (hidden && hidden.matches("input[type=hidden]")) {
        hidden.value = seg.dataset.value || seg.textContent.trim();
        hidden.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
    const topup = event.target.closest(".topup button");
    if (topup) {
      topup.parentElement.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === topup));
      const input = document.getElementById("topup-beans");
      if (input) input.value = topup.dataset.beans;
    }
    const play = event.target.closest(".play[data-src]");
    if (play) {
      if (!play._audio) {
        play._audio = new Audio(play.dataset.src);
        play._audio.addEventListener("ended", () => { play.textContent = "▶"; });
      }
      if (play._audio.paused) {
        play._audio.play();
        play.textContent = "❚❚";
      } else {
        play._audio.pause();
        play.textContent = "▶";
      }
    }
  });

  function drawWave(canvas) {
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    canvas.width = w * devicePixelRatio;
    canvas.height = h * devicePixelRatio;
    const g = canvas.getContext("2d");
    g.scale(devicePixelRatio, devicePixelRatio);
    let seed = Number(canvas.dataset.seed) || 1;
    const rnd = () => (seed = (seed * 9301 + 49297) % 233280) / 233280;
    const bars = Math.floor(w / 4);
    for (let i = 0; i < bars; i++) {
      const env = 0.35 + 0.65 * Math.abs(Math.sin((i / bars) * Math.PI * 3.2));
      const v = Math.max(0.12, env * (0.4 + rnd() * 0.6));
      g.fillStyle = canvas.dataset.dim ? "#2e3550" : i < bars * 0.35 ? "#5c9eff" : "#3a4468";
      g.fillRect(i * 4, (h - v * h) / 2, 2, v * h);
    }
  }

  function drawWaves() {
    document.querySelectorAll("canvas.wave").forEach(drawWave);
  }
  window.addEventListener("resize", drawWaves);
  document.addEventListener("DOMContentLoaded", drawWaves);
  document.body.addEventListener("htmx:afterSettle", drawWaves);

  // We upload straight to the public temp file host, which keeps files for 24 hours.
  const UPLOAD_URL = "https://s.t3ks.com/api/";

  async function uploadFile(picker, file) {
    const target = document.getElementById(picker.dataset.uploadFor);
    const button = picker.closest("label");
    if (!target || !file) return;
    const label = button.firstChild;
    label.textContent = "uploading…";
    const body = new FormData();
    body.append("file", file);
    try {
      const response = await fetch(UPLOAD_URL, { method: "POST", body });
      const data = await response.json();
      if (!response.ok || !data.url) throw new Error(data.message || response.status);
      target.value = data.url;
      target.dispatchEvent(new Event("change", { bubbles: true }));
      label.textContent = "uploaded";
    } catch {
      label.textContent = "upload failed, try again";
    }
    picker.value = "";
  }

  document.addEventListener("change", (event) => {
    if (event.target.matches("input[data-upload-for]")) uploadFile(event.target, event.target.files[0]);
    applyShowWhen();
  });

  // A field that only makes sense for some choices declares them; we hide it and clear it otherwise.
  function applyShowWhen() {
    document.querySelectorAll("[data-show-when]").forEach((field) => {
      const form = field.closest("form");
      const rules = JSON.parse(field.dataset.showWhen);
      const visible = Object.entries(rules).every(([name, value]) => form.elements[name]?.value === value);
      field.hidden = !visible;
      if (!visible) field.querySelectorAll("input, textarea").forEach((input) => { if (input.type !== "file") input.value = ""; });
    });
  }
  document.addEventListener("DOMContentLoaded", applyShowWhen);

  document.addEventListener("dragover", (event) => {
    const zone = event.target.closest(".upload");
    if (!zone) return;
    event.preventDefault();
    zone.classList.add("drop");
  });
  document.addEventListener("dragleave", (event) => {
    event.target.closest(".upload")?.classList.remove("drop");
  });
  document.addEventListener("drop", (event) => {
    const zone = event.target.closest(".upload");
    if (!zone) return;
    event.preventDefault();
    zone.classList.remove("drop");
    const file = event.dataTransfer.files[0];
    if (file) uploadFile(zone.querySelector("input[data-upload-for]"), file);
  });

  function connectQueueStream() {
    const root = document.getElementById("queue-live");
    if (!root || typeof EventSource === "undefined") return;
    const source = new EventSource("/api/queue/stream");
    source.addEventListener("queue", () => {
      htmx.ajax("GET", "/partials/queue", { target: "#queue-live", swap: "innerHTML" });
    });
  }

  function connectJobStream() {
    const root = document.getElementById("job-live");
    if (!root || typeof EventSource === "undefined") return;
    const jobId = root.dataset.jobId;
    const source = new EventSource(`/api/jobs/${jobId}/stream`);
    const refresh = () => htmx.ajax("GET", `/partials/jobs/${jobId}`, { target: "#job-live", swap: "innerHTML" });
    const refreshAndCloseWhenDone = (event) => {
      refresh();
      if (TERMINAL.includes(JSON.parse(event.data).status)) source.close();
    };
    ["step", "log", "result", "error"].forEach((kind) => source.addEventListener(kind, refresh));
    ["snapshot", "status"].forEach((kind) => source.addEventListener(kind, refreshAndCloseWhenDone));
  }

  document.addEventListener("DOMContentLoaded", () => {
    connectQueueStream();
    connectJobStream();
  });
})();
