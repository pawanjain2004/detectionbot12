const statusEl = document.getElementById("status");
const serverUrlInput = document.getElementById("serverUrl");
const saveServerBtn = document.getElementById("saveServer");
const clientIdInput = document.getElementById("clientId");
const robotIdInput = document.getElementById("robotId");
const robotList = document.getElementById("robotList");
const registerClientBtn = document.getElementById("registerClient");
const refreshRobotsBtn = document.getElementById("refreshRobots");
const connectBtn = document.getElementById("connect");
const disconnectBtn = document.getElementById("disconnect");
const videoEl = document.getElementById("video");
const useMjpegEl = document.getElementById("useMjpeg");
const thermalEl = document.getElementById("thermalVideo");
const useMjpegThermalEl = document.getElementById("useMjpegThermal");
const telemetryEl = document.getElementById("telemetry");
const commandInput = document.getElementById("commandInput");
const sendCommandBtn = document.getElementById("sendCommand");
const commandLog = document.getElementById("commandLog");
const logEl = document.getElementById("log");
const driveUpBtn = document.getElementById("driveUp");
const driveDownBtn = document.getElementById("driveDown");
const driveLeftBtn = document.getElementById("driveLeft");
const driveRightBtn = document.getElementById("driveRight");
const driveStopBtn = document.getElementById("driveStop");
const speedDial = document.getElementById("speedDial");
const speedValue = document.getElementById("speedValue");

let videoWs = null;
let thermalWs = null;
let telemetryWs = null;
let commandWs = null;
let firstFrameSeen = false;
const videoState = { pending: false, currentUrl: null };
const thermalState = { pending: false, currentUrl: null };
const pressedKeys = new Set();

function log(message) {
  const line = `[${new Date().toLocaleTimeString()}] ${message}`;
  console.log(line);
  if (logEl) {
    logEl.textContent = `${line}\n${logEl.textContent}`;
  }
}
function getServerBase() {
  const stored = localStorage.getItem("legion_server");
  const value = serverUrlInput.value.trim() || stored || "";
  return value.replace(/\/+$/, "");
}

function getWsBase() {
  const httpBase = getServerBase();
  if (!httpBase) return "";
  return httpBase.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
}

function updateImageFromBuffer(imgEl, buffer, state) {
  if (state.pending) {
    return;
  }
  const blob = new Blob([buffer], { type: "image/jpeg" });
  const nextUrl = URL.createObjectURL(blob);
  state.pending = true;
  imgEl.src = nextUrl;
  if (state.currentUrl) {
    URL.revokeObjectURL(state.currentUrl);
  }
  state.currentUrl = nextUrl;
}

function resetImageState(imgEl, state) {
  state.pending = false;
  if (state.currentUrl) {
    URL.revokeObjectURL(state.currentUrl);
  }
  state.currentUrl = null;
  if (imgEl) {
    imgEl.src = "";
  }
}

function saveServerUrl() {
  const value = serverUrlInput.value.trim().replace(/\/+$/, "");
  if (value) {
    localStorage.setItem("legion_server", value);
    setStatus(`server set: ${value}`);
    log(`server set: ${value}`);
  }
}


function setStatus(text) {
  statusEl.textContent = `Status: ${text}`;
}

function ensureClientId() {
  if (!clientIdInput.value.trim()) {
    clientIdInput.value = `client-${Math.random().toString(16).slice(2, 8)}`;
  }
  return clientIdInput.value.trim();
}

async function registerClient() {
  const clientId = ensureClientId();
  const base = getServerBase();
  if (!base) {
    setStatus("server url required");
    return;
  }
  const res = await fetch(`${base}/api/clients/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: clientId }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    setStatus(`client register failed: ${data.error || res.status}`);
    log(`client register failed: ${data.error || res.status}`);
    return;
  }
  setStatus(`client registered: ${clientId}`);
  log(`client registered: ${clientId}`);
}

async function refreshRobots() {
  const base = getServerBase();
  if (!base) {
    setStatus("server url required");
    return;
  }
  const res = await fetch(`${base}/api/robots?online=1`);
  const data = await res.json();
  robotList.innerHTML = "";
  (data.robots || []).forEach((robot) => {
    const option = document.createElement("option");
    option.value = robot.uuid;
    option.textContent = `${robot.uuid} (${robot.type || "unknown"})`;
    robotList.appendChild(option);
  });
  if (robotList.options.length > 0) {
    robotIdInput.value = robotList.options[0].value;
    log(`online robots: ${robotList.options.length}`);
  } else {
    log("online robots: 0");
  }
}

function connectSockets(robotId) {
  disconnectSockets();
  setStatus(`connecting to ${robotId}`);
  log(`connect requested: ${robotId}`);
  const wsBase = getWsBase();
  if (!wsBase) {
    setStatus("server url required");
    return;
  }
  const useMjpeg = useMjpegEl && useMjpegEl.checked;
  if (useMjpeg) {
    const httpBase = getServerBase();
    const streamUrl = `${httpBase}/mjpeg/${encodeURIComponent(robotId)}?t=${Date.now()}`;
    videoEl.src = streamUrl;
    videoState.pending = false;
    videoState.currentUrl = null;
    setStatus(`video connected: ${robotId} (mjpeg)`);
    log("mjpeg stream connected");
  } else {
    videoWs = new WebSocket(`${wsBase}/ws/video/client/${robotId}`);
    videoWs.binaryType = "arraybuffer";
    videoWs.onmessage = (event) => {
      updateImageFromBuffer(videoEl, event.data, videoState);
    };
    videoWs.onopen = () => {
      setStatus(`video connected: ${robotId}`);
      log("video socket connected");
    };
    videoWs.onclose = () => {
      setStatus("video disconnected");
      log("video socket disconnected");
    };
    videoWs.onerror = () => log("video socket error");
  }

  const useThermalMjpeg = useMjpegThermalEl && useMjpegThermalEl.checked;
  if (useThermalMjpeg) {
    const httpBase = getServerBase();
    const streamUrl = `${httpBase}/mjpeg/thermal/${encodeURIComponent(
      robotId
    )}?t=${Date.now()}`;
    thermalEl.src = streamUrl;
    thermalState.pending = false;
    thermalState.currentUrl = null;
    log("thermal mjpeg stream connected");
  } else {
    thermalWs = new WebSocket(`${wsBase}/ws/thermal/client/${robotId}`);
    thermalWs.binaryType = "arraybuffer";
    thermalWs.onmessage = (event) => {
      updateImageFromBuffer(thermalEl, event.data, thermalState);
    };
    thermalWs.onopen = () => log("thermal socket connected");
    thermalWs.onclose = () => log("thermal socket disconnected");
    thermalWs.onerror = () => log("thermal socket error");
  }

  telemetryWs = new WebSocket(`${wsBase}/ws/telemetry/client/${robotId}`);
  telemetryWs.onmessage = (event) => {
    telemetryEl.textContent = event.data;
    log("telemetry received");
  };
  telemetryWs.onopen = () => {
    setStatus(`telemetry connected: ${robotId}`);
    log("telemetry socket connected");
  };
  telemetryWs.onclose = () => {
    setStatus("telemetry disconnected");
    log("telemetry socket disconnected");
  };
  telemetryWs.onerror = () => log("telemetry socket error");

  commandWs = new WebSocket(`${wsBase}/ws/command/client/${robotId}`);
  commandWs.onmessage = (event) => {
    commandLog.textContent = `[ROBOT] ${event.data}\n` + commandLog.textContent;
    log("command received from robot");
  };
  commandWs.onopen = () => {
    setStatus(`command connected: ${robotId}`);
    log("command socket connected");
  };
  commandWs.onclose = () => {
    setStatus("command disconnected");
    log("command socket disconnected");
  };
  commandWs.onerror = () => log("command socket error");
}

function disconnectSockets() {
  [videoWs, thermalWs, telemetryWs, commandWs].forEach((ws) => {
    if (ws && ws.readyState <= 1) {
      ws.close();
    }
  });
  videoWs = null;
  thermalWs = null;
  telemetryWs = null;
  commandWs = null;
  firstFrameSeen = false;
  resetImageState(videoEl, videoState);
  resetImageState(thermalEl, thermalState);
  setStatus("disconnected");
  log("disconnected");
}

function sendDriveCommand(command) {
  if (!commandWs || commandWs.readyState !== WebSocket.OPEN) {
    return;
  }
  const speed = speedDial ? Number(speedDial.value || 0) : 0;
  const payload = {
    client_id: ensureClientId(),
    command,
    speed,
    ts: Date.now(),
  };
  commandWs.send(JSON.stringify(payload));
  commandLog.textContent = `[CLIENT] ${command} speed=${speed}\n` + commandLog.textContent;
}

function sendCommand() {
  if (!commandWs || commandWs.readyState !== WebSocket.OPEN) {
    setStatus("command socket not connected");
    log("command send failed: socket not connected");
    return;
  }
  const text = commandInput.value.trim();
  if (!text) {
    return;
  }
  const payload = {
    client_id: ensureClientId(),
    command: text,
    ts: Date.now(),
  };
  commandWs.send(JSON.stringify(payload));
  commandLog.textContent = `[CLIENT] ${text}\n` + commandLog.textContent;
  log(`command sent: ${text}`);
  commandInput.value = "";
}

registerClientBtn.addEventListener("click", registerClient);
refreshRobotsBtn.addEventListener("click", refreshRobots);
saveServerBtn.addEventListener("click", saveServerUrl);
connectBtn.addEventListener("click", () => {
  const robotId = robotIdInput.value.trim();
  if (!robotId) {
    setStatus("robot uuid required");
    return;
  }
  connectSockets(robotId);
});
disconnectBtn.addEventListener("click", disconnectSockets);
sendCommandBtn.addEventListener("click", sendCommand);

function bindHoldButton(btn, command, stopCommand) {
  if (!btn) return;
  const start = (event) => {
    event.preventDefault();
    sendDriveCommand(command);
  };
  const stop = (event) => {
    event.preventDefault();
    sendDriveCommand(stopCommand);
  };
  btn.addEventListener("mousedown", start);
  btn.addEventListener("touchstart", start);
  btn.addEventListener("mouseup", stop);
  btn.addEventListener("mouseleave", stop);
  btn.addEventListener("touchend", stop);
  btn.addEventListener("touchcancel", stop);
}

bindHoldButton(driveUpBtn, "MOVE_FORWARD", "FORWARD_STOP");
bindHoldButton(driveDownBtn, "MOVE_BACK", "BACK_STOP");
bindHoldButton(driveLeftBtn, "MOVE_LEFT", "LEFT_STOP");
bindHoldButton(driveRightBtn, "MOVE_RIGHT", "RIGHT_STOP");
if (driveStopBtn) {
  driveStopBtn.addEventListener("click", () => sendDriveCommand("STOP"));
}

commandInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    sendCommand();
  }
});

const storedServer = localStorage.getItem("legion_server");
if (storedServer) {
  serverUrlInput.value = storedServer;
}
if (speedDial && speedValue) {
  speedValue.textContent = speedDial.value;
  speedDial.addEventListener("input", () => {
    speedValue.textContent = speedDial.value;
  });
}
refreshRobots();
log("ui ready");

videoEl.addEventListener("load", () => {
  if (videoState.pending) {
    videoState.pending = false;
  }
  if (!firstFrameSeen) {
    firstFrameSeen = true;
    log("live started: first video frame received");
  }
});

thermalEl.addEventListener("load", () => {
  if (thermalState.pending) {
    thermalState.pending = false;
  }
});

const keyDownCommands = {
  w: "MOVE_FORWARD",
  s: "MOVE_BACK",
  a: "MOVE_LEFT",
  d: "MOVE_RIGHT",
};
const keyUpCommands = {
  w: "FORWARD_STOP",
  s: "BACK_STOP",
  a: "LEFT_STOP",
  d: "RIGHT_STOP",
};

document.addEventListener("keydown", (event) => {
  const tag = (document.activeElement && document.activeElement.tagName) || "";
  if (["INPUT", "TEXTAREA"].includes(tag)) {
    return;
  }
  const key = event.key.toLowerCase();
  if (!["w", "a", "s", "d"].includes(key)) {
    return;
  }
  if (pressedKeys.has(key)) {
    return;
  }
  event.preventDefault();
  pressedKeys.add(key);
  sendDriveCommand(keyDownCommands[key]);
});

document.addEventListener("keyup", (event) => {
  const key = event.key.toLowerCase();
  if (!["w", "a", "s", "d"].includes(key)) {
    return;
  }
  event.preventDefault();
  pressedKeys.delete(key);
  sendDriveCommand(keyUpCommands[key]);
});

