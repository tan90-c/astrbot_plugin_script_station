// 脚本挂机面板前端
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
await bridge.ready();

// ---------- 状态 ----------
let scripts = [];            // 服务端全量列表
let active = "home";         // 'home' 或 script id
let dirty = false;           // 编辑器未保存标记
const maxLinesDom = 2000;    // 控制台 DOM 行数上限
let pollBusy = false;

const $ = (id) => document.getElementById(id);

// ---------- 工具 ----------
function toast(msg, kind = "ok") {
  const el = $("toast");
  el.textContent = msg;
  el.className = kind;
  el.style.display = "block";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.style.display = "none"; }, 2600);
}

function askConfirm(msg) {
  return new Promise((resolve) => {
    $("modalMsg").textContent = msg;
    $("modalMask").style.display = "flex";
    const done = (v) => { $("modalMask").style.display = "none"; resolve(v); };
    $("modalYes").onclick = () => done(true);
    $("modalNo").onclick = () => done(false);
  });
}

function fmtTime(t) {
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

async function apiPost(path, body) {
  const res = await bridge.apiPost(path, body);
  if (res && res.ok === false) {
    toast(res.message || "操作失败", "err");
    throw new Error(res.message || "api error");
  }
  return res;
}

const byId = (id) => scripts.find((s) => s.id === id);

// ---------- 标签页 ----------
function renderTabs() {
  const box = $("tabs");
  box.innerHTML = "";
  const mk = (key, label, dotClass) => {
    const el = document.createElement("div");
    el.className = "tab" + (active === key ? " active" : "");
    if (dotClass) {
      const dot = document.createElement("span");
      dot.className = "dot " + dotClass;
      el.appendChild(dot);
    }
    const txt = document.createElement("span");
    txt.textContent = label;
    el.appendChild(txt);
    el.onclick = () => switchTab(key);
    box.appendChild(el);
  };
  mk("home", "首页");
  for (const s of scripts) {
    const st = s.status || {};
    const dot = st.running ? (st.stopping ? "stopping" : "running")
      : (st.exitCode != null && st.exitCode !== 0 ? "exited" : "");
    mk(s.id, s.name, dot);
  }
}

function switchTab(key) {
  if (active === key) return;
  if (active !== "home" && dirty) {
    // 有未保存修改时切换标签：保留在 state 里（服务端为准，提示用户）
    toast("有未保存的代码修改，切回该标签前请先保存（切换将丢失编辑框内容）", "err");
  }
  active = key;
  const isHome = key === "home";
  $("view-home").classList.toggle("hidden", !isHome);
  $("view-script").classList.toggle("hidden", isHome);
  if (!isHome) {
    const s = byId(key);
    $("scName").value = s ? s.name : "";
    $("scCode").value = s ? s.code : "";
    $("scAuto").checked = !!(s && s.autoStart);
    dirty = false;
    $("console").innerHTML = "";
    renderScriptStatus();
    pollLogs(true);
  }
  renderTabs();
  renderLibrary();
}

// ---------- 首页脚本库 ----------
function renderLibrary() {
  const tbody = $("libBody");
  tbody.innerHTML = "";
  $("libEmpty").classList.toggle("hidden", scripts.length > 0);
  for (const s of scripts) {
    const st = s.status || {};
    const tr = document.createElement("tr");

    const tdName = document.createElement("td");
    tdName.textContent = s.name;
    tr.appendChild(tdName);

    const tdState = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = "pill " + (st.running ? "running"
      : st.exitCode != null ? (st.exitCode === 0 ? "" : "exited") : "");
    pill.textContent = st.running ? "运行中"
      : st.exitCode != null ? `已退出(${st.exitCode})` : "已停止";
    tdState.appendChild(pill);
    tr.appendChild(tdState);

    const tdAuto = document.createElement("td");
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = !!s.autoStart;
    chk.onchange = async () => {
      try {
        await apiPost("scripts/save", { script: { id: s.id, name: s.name, code: s.code, autoStart: chk.checked } });
        toast(chk.checked ? "已开启自动启动" : "已关闭自动启动");
        await refresh();
      } catch { chk.checked = s.autoStart; }
    };
    tdAuto.appendChild(chk);
    tr.appendChild(tdAuto);

    const tdOps = document.createElement("td");
    const ops = document.createElement("div");
    ops.className = "ops";
    const mkBtn = (text, cls, fn) => {
      const b = document.createElement("button");
      b.textContent = text;
      b.className = cls;
      b.onclick = fn;
      ops.appendChild(b);
    };
    mkBtn("打开", "", () => switchTab(s.id));
    mkBtn(st.running ? "停止" : "启动", st.running ? "stop" : "run", async () => {
      try {
        await apiPost(st.running ? "stop" : "run", { id: s.id });
        toast(st.running ? "已发送停止指令" : "已启动");
        await refresh();
      } catch {}
    });
    mkBtn("删除", "danger", async () => {
      if (!await askConfirm(`确定删除脚本「${s.name}」？运行中的进程会被停止，且不可恢复。`)) return;
      try {
        await apiPost("scripts/delete", { id: s.id });
        if (active === s.id) switchTab("home");
        toast("已删除");
        await refresh();
      } catch {}
    });
    tdOps.appendChild(ops);
    tr.appendChild(tdOps);

    tbody.appendChild(tr);
  }
}

// ---------- 脚本详情 ----------
function renderScriptStatus() {
  const s = byId(active);
  if (!s) return;
  const st = s.status || {};
  const pill = $("scState");
  const state = st.running ? "running" : (st.exitCode != null ? (st.exitCode === 0 ? "stopped" : "exited") : "stopped");
  pill.className = "pill " + (state === "running" ? "running" : state === "exited" ? "exited" : "");
  pill.textContent = st.running ? `运行中 (pid ${st.pid || "-"})`
    : st.exitCode != null ? `已退出 (code ${st.exitCode})` : "已停止";
  $("btnRun").disabled = !!st.running;
  $("btnStop").disabled = !st.running;
  $("scHint").textContent = dirty ? "● 有未保存修改" : "";
}

function appendConsole(lines) {
  const box = $("console");
  const follow = $("conFollow").checked;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  for (const ln of lines) {
    const span = document.createElement("span");
    span.className = "line" + (ln.text.startsWith("[面板]") ? " sys" : "");
    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = fmtTime(ln.t);
    span.appendChild(ts);
    span.appendChild(document.createTextNode(ln.text));
    box.appendChild(span);
  }
  while (box.childElementCount > maxLinesDom) box.removeChild(box.firstChild);
  if (follow && atBottom) box.scrollTop = box.scrollHeight;
}

let logsOffset = 0;
async function pollLogs(reset = false) {
  if (active === "home" || pollBusy) return;
  pollBusy = true;
  try {
    if (reset) logsOffset = 0;
    const res = await apiPost("logs", { id: active, offset: logsOffset });
    if (active === "home") return; // 轮询期间切走了
    if (res.reset) $("console").innerHTML = "";
    if (res.lines && res.lines.length) appendConsole(res.lines);
    logsOffset = res.next;
    if (res.status) {
      const s = byId(active);
      if (s) { s.status = res.status; renderScriptStatus(); renderTabs(); }
    }
  } catch { /* 网络抖动忽略，下轮重试 */ }
  finally { pollBusy = false; }
}

// ---------- 按钮 ----------
$("btnAdd").onclick = async () => {
  const name = $("newName").value.trim();
  const code = $("newCode").value;
  if (!name) return toast("请填写脚本名称", "err");
  if (!code.trim()) return toast("请填写脚本代码", "err");
  try {
    const res = await apiPost("scripts/save", {
      script: { name, code, autoStart: $("newAuto").checked },
    });
    $("newName").value = "";
    $("newCode").value = "";
    $("newAuto").checked = false;
    toast("已添加脚本");
    await refresh();
    if (res && res.id) switchTab(res.id);
  } catch {}
};

$("btnSave").onclick = async () => {
  const s = byId(active);
  if (!s) return;
  try {
    await apiPost("scripts/save", {
      script: { id: s.id, name: $("scName").value.trim(), code: $("scCode").value, autoStart: $("scAuto").checked },
    });
    dirty = false;
    toast("已保存");
    await refresh();
  } catch {}
};

$("btnRun").onclick = async () => {
  const s = byId(active);
  if (!s) return;
  // 运行前自动保存当前编辑器内容，避免跑旧代码
  try {
    await apiPost("scripts/save", {
      script: { id: s.id, name: $("scName").value.trim(), code: $("scCode").value, autoStart: $("scAuto").checked },
    });
    dirty = false;
    await apiPost("run", { id: s.id });
    $("console").innerHTML = "";
    logsOffset = 0;
    toast("已启动");
    await refresh();
  } catch {}
};

$("btnStop").onclick = async () => {
  try {
    await apiPost("stop", { id: active });
    toast("已发送停止指令");
    await refresh();
  } catch {}
};

$("btnDel").onclick = async () => {
  const s = byId(active);
  if (!s) return;
  if (!await askConfirm(`确定删除脚本「${s.name}」？运行中的进程会被停止，且不可恢复。`)) return;
  try {
    await apiPost("scripts/delete", { id: s.id });
    toast("已删除");
    active = "__gone__"; // 强制 switchTab 到 home 生效
    switchTab("home");
    await refresh();
  } catch {}
};

$("btnClearLog").onclick = async () => {
  try {
    await apiPost("clear_log", { id: active });
    $("console").innerHTML = "";
    logsOffset = 0;
  } catch {}
};

$("scCode").addEventListener("input", () => { dirty = true; renderScriptStatus(); });
$("scName").addEventListener("input", () => { dirty = true; renderScriptStatus(); });
$("scAuto").addEventListener("change", () => { dirty = true; renderScriptStatus(); });
// Tab 键在代码框内插入缩进
$("scCode").addEventListener("keydown", (e) => {
  if (e.key === "Tab") {
    e.preventDefault();
    const el = e.target;
    const st = el.selectionStart;
    el.setRangeText("    ", st, el.selectionEnd, "end");
    dirty = true;
  }
});
new ResizeObserver(() => {
  // 面板宽度变化时保持控制台贴底
  const box = $("console");
  if ($("conFollow").checked && box.scrollHeight - box.scrollTop - box.clientHeight < 80) {
    box.scrollTop = box.scrollHeight;
  }
}).observe($("console"));

// ---------- 轮询 ----------
async function refresh() {
  try {
    const res = await bridge.apiGet("scripts");
    if (res && res.ok) {
      scripts = res.scripts || [];
      renderTabs();
      if (active === "home") renderLibrary();
      else {
        renderScriptStatus();
        const s = byId(active);
        if (!s) { active = "__gone__"; switchTab("home"); }
      }
    }
  } catch {}
}

setInterval(refresh, 2500);
setInterval(() => pollLogs(false), 1200);

await refresh();
renderTabs();
renderLibrary();
