// 脚本挂机面板 v2 — 项目文件夹式
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
await bridge.ready();

// ---------- 状态 ----------
let projects = [];
let baseDir = null;               // 默认目录 data/plugin_data/<插件名>
let active = "home";              // 'home' | project id
let currentFile = null;           // 项目内相对路径
const fileStates = {};            // `${pid}|${rel}` -> {code, dirty}
const fileCounts = {};            // pid -> N（文件树缓存计数）
const folds = {};                 // `${pid}|${dir}` -> true(收起)
const logOffsets = {};            // pid -> cursor
let logsBusy = false;
const MAX_DOM = 2000;

const $ = (id) => document.getElementById(id);
const esc = (s) => s; // 用 textContent 渲染，无需转义

function toast(msg, kind = "ok") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "show " + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.className = ""; }, 2600);
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

async function apiPost(path, body) {
  const res = await bridge.apiPost(path, body);
  if (res && res.ok === false) { toast(res.message || "操作失败", "err"); throw new Error(res.message); }
  return res;
}

function fmtTime(t) {
  const d = new Date(t * 1000), p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const projById = (id) => projects.find((p) => p.id === id);
const st = () => fileStates[`${active}|${currentFile}`];
const IMG_EXT = ["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"];
const extOf = (rel) => (rel.split(".").pop() || "").toLowerCase();
const iconOf = (rel) => {
  const e = extOf(rel);
  if (e === "py") return "🐍";
  if (IMG_EXT.includes(e)) return "🖼";
  if (["json", "yaml", "yml", "toml", "ini", "conf", "env", "lock"].includes(e)) return "⚙️";
  if (["md", "txt", "log", "csv", "html", "htm", "js", "ts", "css", "xml", "bat", "sh", "ps1"].includes(e)) return "📄";
  return "📦";
};
const fmtSize = (n) => n >= 1024 * 1024 ? (n / 1048576).toFixed(2) + " MB" : (n / 1024).toFixed(1) + " KB";

// ---------- 侧栏 ----------
function renderTabs() {
  const list = $("tabList");
  list.innerHTML = "";
  const q = $("searchBox").value.trim().toLowerCase();
  const mk = (key, avatar, name, sub, running, isHome) => {
    const el = document.createElement("div");
    el.className = "tab" + (isHome ? " home" : "") + (active === key ? " active" : "");
    const av = document.createElement("div");
    av.className = "avatar"; av.textContent = avatar;
    const meta = document.createElement("div"); meta.className = "meta";
    const t1 = document.createElement("div"); t1.className = "tname"; t1.textContent = name;
    const t2 = document.createElement("div"); t2.className = "tsub"; t2.textContent = sub;
    meta.appendChild(t1); meta.appendChild(t2);
    el.appendChild(av); el.appendChild(meta);
    if (!isHome) {
      const dot = document.createElement("span");
      dot.className = "dot" + (running ? " on" : "");
      el.appendChild(dot);
    }
    el.onclick = () => switchTab(key);
    list.appendChild(el);
  };
  mk("home", "🏠", "首页", "添加项目文件夹 / 使用说明", 0, true);
  for (const p of projects) {
    if (q && !p.name.toLowerCase().includes(q) && !p.path.toLowerCase().includes(q)) continue;
    const n = p.status ? p.status.running : 0;
    const sub = !p.valid ? "⚠ 路径失效" : n ? `运行 ${n} 个脚本` : `${fileCounts[p.id] ?? "-"} 个脚本 · 空闲`;
    mk(p.id, "📁", p.name, sub, n, false);
  }
}

function switchTab(key) {
  if (active === key) return;
  active = key;
  const isHome = key === "home";
  $("homeView").style.display = isHome ? "" : "none";
  $("projView").style.display = isHome ? "none" : "";
  if (!isHome) {
    currentFile = null;
    $("console").innerHTML = "";
    logOffsets[key] = 0;
    renderProjectHead();
    renderEditor();
    loadFiles(key);
    pollLogs(key, true);
  }
  renderTabs();
  renderProjTable();
}

// ---------- 首页 ----------
function renderProjTable() {
  const tb = $("projTable");
  tb.innerHTML = "";
  $("projEmpty").style.display = projects.length ? "none" : "";
  for (const p of projects) {
    const tr = document.createElement("tr");
    const td = (html) => { const c = document.createElement("td"); c.textContent = html; tr.appendChild(c); return c; };
    td(p.name);
    td(p.path);
    const tdS = td("");
    const pill = document.createElement("span");
    const n = p.status ? p.status.running : 0;
    pill.className = "pill" + (n ? " on" : "") + (p.valid ? "" : " bad");
    pill.textContent = !p.valid ? "路径失效" : n ? `运行 ${n}` : "空闲";
    tdS.appendChild(pill);
    const tdO = document.createElement("td");
    const mkB = (t, cls, fn) => { const b = document.createElement("button"); b.className = "btn small " + cls; b.textContent = t; b.onclick = fn; tdO.appendChild(b); };
    mkB("打开", "secondary", () => switchTab(p.id));
    mkB("删除", "danger", async () => {
      if (!await askConfirm(`删除项目「${p.name}」登记？运行中的脚本会被停止，你的文件夹和文件不会被删除。`)) return;
      try {
        await apiPost("projects/delete", { id: p.id });
        if (active === p.id) switchTab("home");
        toast("已删除（文件保留）");
        await refresh();
      } catch {}
    });
    tr.appendChild(tdO);
    tb.appendChild(tr);
  }
}

$("btnPlus").onclick = () => { switchTab("home"); $("projName").focus(); };
$("searchBox").addEventListener("input", renderTabs);

$("btnAddProj").onclick = async () => {
  const name = $("projName").value.trim();
  const path = $("projPath").value.trim();
  if (!name) return toast("请填写项目名", "err");
  // 路径留空：服务端在默认目录下按项目名自动建文件夹
  try {
    const r = await apiPost("projects/save", { project: { name, path } });
    $("projName").value = ""; $("projPath").value = "";
    toast(path ? "项目已创建" : "已在默认目录创建项目文件夹");
    await refresh();
    if (r.id) switchTab(r.id);
  } catch {}
};

// ---------- 目录浏览 ----------
let browseAt = null, browseParent = undefined, browseAtBase = false;
async function openBrowse() {
  $("browseMask").style.display = "flex";
  await browseTo(null);   // 默认从 plugin_data 目录开始
}
async function browseTo(path) {
  try {
    const r = await apiPost("browse", path === null ? {} : { path });
    browseAt = r.at; browseParent = r.parent; browseAtBase = !!r.at_base;
    baseDir = r.base || baseDir;
    $("browsePath").textContent = (r.at || "（选择盘符）") + (r.at_base ? "　【默认目录】" : "");
    $("brUp").disabled = !r.parent;
    $("btnMkDir").disabled = !r.at;
    $("brPick").disabled = !r.at;
    const box = $("browseList");
    box.innerHTML = "";
    if (!r.dirs.length) {
      const d = document.createElement("div"); d.className = "bd"; d.textContent = "（没有子文件夹）"; d.style.cursor = "default";
      box.appendChild(d);
    }
    for (const d of r.dirs) {
      const row = document.createElement("div");
      row.className = "bd";
      row.textContent = "📁 " + d.name;
      row.onclick = () => browseTo(d.path);
      box.appendChild(row);
    }
  } catch { $("browseMask").style.display = "none"; }
}
$("btnBrowse").onclick = openBrowse;
$("brUp").onclick = () => { if (browseParent) browseTo(browseParent); };
$("brHome").onclick = () => browseTo(null);
$("brDrives").onclick = () => browseTo("");
$("btnMkDir").onclick = async () => {
  const name = $("brNewName").value.trim();
  if (!name) return toast("请输入新文件夹名", "err");
  try {
    const r = await apiPost("mkdir", { path: browseAt, name });
    $("brNewName").value = "";
    toast("已创建，进入：" + r.path);
    await browseTo(r.path);
  } catch {}
};
$("brCancel").onclick = () => { $("browseMask").style.display = "none"; };
$("brPick").onclick = () => {
  if (!browseAt) return toast("请先进入一个文件夹", "err");
  $("projPath").value = browseAt;
  if (!$("projName").value.trim()) $("projName").value = browseAt.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "";
  $("browseMask").style.display = "none";
};

// ---------- 项目视图 ----------
function renderProjectHead() {
  const p = projById(active);
  if (!p) return;
  $("pvTitle").textContent = p.name;
  $("pvPath").textContent = p.path;
  $("pvPath").title = p.path;
  const n = p.status ? p.status.running : 0;
  const pill = $("pvState");
  pill.className = "pill" + (n ? " on" : "") + (p.valid ? "" : " bad");
  pill.textContent = !p.valid ? "⚠ 路径失效" : n ? `运行 ${n} 个进程` : "空闲";
  $("btnStopAll").disabled = !n;
  const meta = $("conMeta");
  meta.textContent = p.status && p.status.procs.length
    ? "运行中: " + p.status.procs.map((x) => `${x.file}(pid ${x.pid})`).join("  ")
    : "无运行中的脚本";
}

async function loadFiles(pid) {
  try {
    const r = await apiPost("files", { id: pid });
    if (pid !== active) return;
    fileCounts[pid] = r.files.length;
    renderTree(pid, r.files, r.truncated);
    renderTabs();
  } catch (e) {
    if (pid === active) $("fileTree").innerHTML = "";
    if (pid === active) { const d = document.createElement("div"); d.className = "empty"; d.textContent = "无法读取文件树"; $("fileTree").appendChild(d); }
  }
}

function renderTree(pid, files, truncated) {
  const box = $("fileTree");
  box.innerHTML = "";
  if (!files.length) {
    const d = document.createElement("div"); d.className = "empty"; d.textContent = "文件夹内没有 .py 文件";
    box.appendChild(d); return;
  }
  // 构建树
  const root = { dirs: {}, files: [] };
  for (const f of files) {
    const parts = f.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) node = node.dirs[parts[i]] ??= { dirs: {}, files: [] };
    node.files.push({ name: parts[parts.length - 1], rel: f });
  }
  const walk = (node, prefix, depth) => {
    for (const [name, child] of Object.entries(node.dirs)) {
      const dirKey = `${pid}|${prefix}${name}`;
      const folded = folds[dirKey];
      const row = document.createElement("div");
      row.className = "node folder";
      row.style.paddingLeft = `${6 + depth * 14}px`;
      row.textContent = `${folded ? "▸" : "▾"} 📁 ${name}`;
      row.onclick = () => { folds[dirKey] = !folded; renderTree(pid, files, truncated); };
      box.appendChild(row);
      if (!folded) walk(child, `${prefix}${name}/`, depth + 1);
    }
    for (const f of node.files) {
      const row = document.createElement("div");
      row.className = "node" + (currentFile === f.rel ? " sel" : "");
      row.style.paddingLeft = `${20 + depth * 14}px`;
      row.textContent = iconOf(f.rel) + " " + f.name;
      row.title = f.rel;
      row.onclick = () => selectFile(f.rel);
      box.appendChild(row);
    }
  };
  walk(root, "", 0);
  if (truncated) {
    const d = document.createElement("div"); d.className = "hint"; d.textContent = `文件过多，仅显示前 ${files.length} 个`;
    box.appendChild(d);
  }
}

async function selectFile(rel) {
  const key = `${active}|${rel}`;
  if (!fileStates[key]) {
    try {
      const r = await apiPost("read_file", { id: active, file: rel });
      if (r.binary) {
        if (IMG_EXT.includes(extOf(rel))) {
          try {
            const pv = await apiPost("preview", { id: active, file: rel });
            fileStates[key] = { type: "image", url: pv.dataUrl, size: pv.size };
          } catch { fileStates[key] = { type: "binary", size: r.size }; }
        } else {
          fileStates[key] = { type: "binary", size: r.size };
        }
      } else if (r.tooBig) {
        fileStates[key] = { type: "toolarge", size: r.size };
      } else {
        fileStates[key] = { type: "text", code: r.content, dirty: false, size: r.size };
      }
    } catch { return; }
  }
  currentFile = rel;
  renderEditor();
  loadFiles(active);   // 重绘选中态
}

function renderEditor() {
  const s = st();
  $("edFile").textContent = currentFile || "未选择文件";
  const isText = !!s && s.type === "text";
  const isImg = !!s && s.type === "image";
  const isBin = !!s && s.type === "binary";
  const isPy = !!currentFile && extOf(currentFile) === "py";
  $("edCode").style.display = s && !isText ? "none" : "";
  $("edCode").value = isText ? s.code : "";
  $("edCode").disabled = !isText;
  $("previewBox").style.display = isImg ? "" : "none";
  if (isImg) {
    $("previewImg").src = s.url;
    $("previewInfo").textContent = `${currentFile} · ${fmtSize(s.size)}（图片预览，不支持编辑）`;
  }
  $("binInfo").style.display = isBin || (s && s.type === "toolarge") ? "" : "none";
  if (isBin) $("binInfo").textContent = `二进制文件（${fmtSize(s.size)}），不支持编辑/预览/运行；仍可删除`;
  else if (s && s.type === "toolarge") $("binInfo").textContent = `文件过大（${fmtSize(s.size)}，文本编辑上限 200KB），不支持编辑/预览/运行；仍可删除`;
  $("edDirty").textContent = isText && s.dirty ? "● 未保存" : "";
  $("btnSaveFile").disabled = !isText || !s.dirty;
  $("btnDelFile").disabled = !currentFile;
  const p = projById(active);
  const running = !!(p && p.status && p.status.procs.some((x) => x.file === currentFile));
  $("btnRunFile").disabled = !isPy || running || !p || !p.valid;
  $("btnStopFile").disabled = !running;
  $("btnAutoRun").disabled = !isPy || !p;
  if (p && currentFile && isPy) {
    $("btnAutoRun").textContent = p.autoRun.includes(currentFile) ? "取消自动运行" : "设为自动运行";
    $("btnAutoRun").classList.toggle("secondary", true);
  }
}

$("edCode").addEventListener("input", () => {
  const s = st();
  if (s) { s.code = $("edCode").value; s.dirty = true; renderEditor(); }
});
$("edCode").addEventListener("keydown", (e) => {
  if (e.key === "Tab" && st()) {
    e.preventDefault();
    const el = e.target, pos = el.selectionStart;
    el.setRangeText("    ", pos, el.selectionEnd, "end");
    const s = st(); s.code = el.value; s.dirty = true; renderEditor();
  }
  if (e.key === "s" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("btnSaveFile").click(); }
});

async function saveCurrent() {
  const s = st();
  if (!s || s.type !== "text" || !s.dirty) return true;
  try {
    await apiPost("write_file", { id: active, file: currentFile, content: s.code });
    s.dirty = false;
    renderEditor();
    return true;
  } catch { return false; }
}

$("btnSaveFile").onclick = async () => { if (await saveCurrent()) toast("已保存"); };

$("btnDelFile").onclick = async () => {
  if (!currentFile) return;
  if (!await askConfirm(`删除文件「${currentFile}」？此操作会真实删除磁盘文件，不可恢复！`)) return;
  try {
    await apiPost("delete_file", { id: active, file: currentFile });
    delete fileStates[`${active}|${currentFile}`];
    const p = projById(active);
    if (p) p.autoRun = p.autoRun.filter((x) => x !== currentFile);
    toast("已删除文件");
    currentFile = null;
    renderEditor();
    loadFiles(active);
  } catch {}
};

$("btnRunFile").onclick = async () => {
  if (!await saveCurrent()) return;
  try {
    await apiPost("run", { id: active, file: currentFile });
    toast("已启动 " + currentFile);
    await refresh();
    renderEditor();
  } catch {}
};

$("btnStopFile").onclick = async () => {
  const p = projById(active);
  const proc = p && p.status.procs.find((x) => x.file === currentFile);
  if (!proc) return;
  try {
    await apiPost("stop", { id: active, runId: proc.runId });
    toast("已停止");
    await refresh();
    renderEditor();
  } catch {}
};

$("btnStopAll").onclick = async () => {
  try {
    await apiPost("stop", { id: active });
    toast("已停止全部");
    await refresh();
  } catch {}
};

$("btnAutoRun").onclick = async () => {
  const p = projById(active);
  if (!p || !currentFile) return;
  const list = p.autoRun.includes(currentFile)
    ? p.autoRun.filter((x) => x !== currentFile)
    : [...p.autoRun, currentFile];
  try {
    await apiPost("projects/save", { project: { id: p.id, name: p.name, path: p.path, autoRun: list } });
    toast(list.includes(currentFile) ? "已设为自动运行（插件加载时自动启动）" : "已取消自动运行");
    await refresh();
    renderEditor();
  } catch {}
};

$("btnRename").onclick = () => {
  const h = $("pvTitle");
  const p = projById(active);
  if (!p || h.tagName === "INPUT") return;
  const inp = document.createElement("input");
  inp.value = p.name; inp.style.width = "220px";
  h.replaceWith(inp); inp.focus(); inp.select();
  const finish = async (ok) => {
    const v = inp.value.trim();
    inp.replaceWith(h);
    if (ok && v && v !== p.name) {
      try {
        await apiPost("projects/save", { project: { id: p.id, name: v, path: p.path, autoRun: p.autoRun } });
        toast("已改名"); await refresh();
      } catch {}
    }
  };
  inp.onblur = () => finish(true);
  inp.onkeydown = (e) => { if (e.key === "Enter") inp.blur(); if (e.key === "Escape") { inp.value = p.name; inp.blur(); } };
};

$("btnDelProj").onclick = async () => {
  const p = projById(active);
  if (!p) return;
  if (!await askConfirm(`删除项目「${p.name}」登记？运行中的脚本会被停止，你的文件夹和文件不会被删除。`)) return;
  try {
    await apiPost("projects/delete", { id: p.id });
    toast("已删除（文件保留）");
    switchTab("home");
    await refresh();
  } catch {}
};

$("btnRefreshFiles").onclick = () => { if (active !== "home") loadFiles(active); };
$("btnClear").onclick = async () => {
  try {
    await apiPost("clear_log", { id: active });
    $("console").innerHTML = "";
    logOffsets[active] = 0;
  } catch {}
};

// ---------- 控制台 ----------
function appendConsole(pid, lines) {
  const box = $("console");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  for (const ln of lines) {
    const span = document.createElement("span");
    span.className = "line" + (ln.text.startsWith("[面板]") ? " sys" : "");
    const ts = document.createElement("span"); ts.className = "ts"; ts.textContent = fmtTime(ln.t);
    const tg = document.createElement("span"); tg.className = "tag"; tg.textContent = `[${ln.tag}]`;
    span.appendChild(ts); span.appendChild(tg);
    span.appendChild(document.createTextNode(" " + ln.text));
    box.appendChild(span);
  }
  while (box.childElementCount > MAX_DOM) box.removeChild(box.firstChild);
  if ($("conFollow").checked && atBottom) box.scrollTop = box.scrollHeight;
}

async function pollLogs(pid, reset) {
  if (active !== pid || logsBusy) return;
  logsBusy = true;
  try {
    if (reset) logOffsets[pid] = 0;
    const r = await apiPost("logs", { id: pid, offset: logOffsets[pid] || 0 });
    if (active !== pid) return;
    if (r.reset) $("console").innerHTML = "";
    if (r.lines.length) appendConsole(pid, r.lines);
    logOffsets[pid] = r.next;
    const p = projById(pid);
    if (p && r.status) { p.status = r.status; renderProjectHead(); renderEditor(); }
  } catch {} finally { logsBusy = false; }
}

// ---------- 轮询 ----------
async function refresh() {
  try {
    const res = await bridge.apiGet("projects");
    if (!res || !res.ok) return;
    projects = res.projects || [];
    if (res.base) {
      baseDir = res.base;
      $("projPath").placeholder = "留空=在默认目录创建：" + baseDir;
    }
    renderTabs();
    if (active === "home") renderProjTable();
    else {
      const p = projById(active);
      if (!p) { active = "home"; switchTab("home"); return; }
      renderProjectHead();
      renderEditor();
    }
  } catch {}
}

setInterval(refresh, 2500);
setInterval(() => { if (active !== "home") pollLogs(active, false); }, 1200);

await refresh();
renderTabs();
renderProjTable();
