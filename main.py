# -*- coding: utf-8 -*-
"""astrbot_plugin_script_station v2 — 脚本挂机面板（项目文件夹版）

1. 首页添加「项目文件夹」（本机绝对路径，带目录浏览选择器）；
2. 每个项目一个标签页：左侧 .py 文件树，点文件即可编辑/运行；
3. 每个项目一个共享控制台：所有运行脚本的 stdout/stderr 汇入，带文件名前缀；
4. 常驻脚本支持：python -u 子进程持续运行（app.run 等），停止杀进程树；
5. 自动运行：项目内标记 autoRun 的文件在插件加载时自动拉起。

脚本文件保存在用户自己的项目目录里（不再存配置）；配置只存项目登记，
删除项目只停止进程并移除登记，绝不删除用户文件。
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import time
import uuid

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # 部分版本无 astrbot.api.web：Quart 回退
    import json as _json

    try:
        from quart import Response as _QR
        from quart import request as _qr
    except ImportError:  # pragma: no cover
        _QR = None
        _qr = None

    def json_response(data, status=200, **kw):
        if _QR is not None:
            return _QR(
                _json.dumps(data, ensure_ascii=False, default=str),
                status=status,
                mimetype="application/json",
            )
        return {"__json__": data, "__status__": status}

    def error_response(message, status_code=400, **kw):
        return json_response({"ok": False, "message": message}, status=status_code)

    class _ReqProxy:
        @property
        def method(self):
            return _qr.method if _qr is not None else "POST"

        async def json(self, default=None):
            if _qr is None:
                return default
            try:
                d = await _qr.get_json(silent=True)
            except Exception:
                d = None
            return default if d is None else d

    request = _ReqProxy()

PLUGIN_ID = "astrbot_plugin_script_station"
VERSION = "2.0.0"

MAX_NAME = 64
MAX_FILE_BYTES = 200_000   # 单脚本读/写上限
MAX_LINES = 3000           # 每项目控制台保留行数
MAX_TREE_FILES = 800       # 文件树最多返回文件数
MAX_TREE_DEPTH = 6
MAX_BROWSE_DIRS = 400
ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
SKIP_DIRS = {
    "__pycache__", ".git", ".svn", ".hg", "node_modules", ".venv", "venv",
    "env", ".idea", ".vscode", "dist", "build", ".tox", ".mypy_cache",
    ".pytest_cache", "__tmp__",
}


class ProcHandle:
    """项目内一次脚本运行。"""

    def __init__(self, run_id: str, rel_file: str, proc):
        self.run_id = run_id
        self.file = rel_file            # 相对项目根的 posix 风格路径
        self.proc = proc
        self.reader: asyncio.Task | None = None
        self.waiter: asyncio.Task | None = None
        self.started_at = time.time()

    def brief(self) -> dict:
        return {
            "runId": self.run_id,
            "file": self.file,
            "pid": self.proc.pid,
            "startedAt": self.started_at,
        }


class ProjectConsole:
    """每项目共享控制台缓冲 + 进程集合。"""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.lines: list[dict] = []     # {seq,t,tag,text}
        self.seq = 0
        self.procs: dict[str, ProcHandle] = {}
        self.lock = asyncio.Lock()

    def append(self, tag: str, text: str) -> None:
        self.seq += 1
        self.lines.append({"seq": self.seq, "t": time.time(), "tag": tag, "text": text})
        if len(self.lines) > MAX_LINES:
            del self.lines[: len(self.lines) - MAX_LINES]

    def status(self) -> dict:
        return {
            "running": len(self.procs),
            "procs": [p.brief() for p in self.procs.values()],
            "lines": len(self.lines),
            "cursor": self.seq,
        }


@register(PLUGIN_ID, "tan90-c", "项目文件夹式 Python 脚本挂机面板", VERSION,
          "https://github.com/tan90-c/astrbot_plugin_script_station")
class ScriptStation(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self.consoles: dict[str, ProjectConsole] = {}
        if hasattr(context, "register_web_api"):
            a = context.register_web_api
            a(f"/{PLUGIN_ID}/projects", self.web_projects, ["GET"], "项目列表与状态")
            a(f"/{PLUGIN_ID}/projects/save", self.web_save_project, ["POST"], "新增/更新项目")
            a(f"/{PLUGIN_ID}/projects/delete", self.web_delete_project, ["POST"], "删除项目(不删文件)")
            a(f"/{PLUGIN_ID}/browse", self.web_browse, ["POST"], "浏览本机目录")
            a(f"/{PLUGIN_ID}/files", self.web_files, ["POST"], "项目内 .py 文件树")
            a(f"/{PLUGIN_ID}/read_file", self.web_read_file, ["POST"], "读取脚本")
            a(f"/{PLUGIN_ID}/write_file", self.web_write_file, ["POST"], "保存脚本")
            a(f"/{PLUGIN_ID}/run", self.web_run, ["POST"], "运行脚本文件")
            a(f"/{PLUGIN_ID}/stop", self.web_stop, ["POST"], "停止一个/全部进程")
            a(f"/{PLUGIN_ID}/logs", self.web_logs, ["POST"], "增量拉取控制台")
            a(f"/{PLUGIN_ID}/clear_log", self.web_clear_log, ["POST"], "清空控制台")
        else:  # pragma: no cover
            logger.warning("[%s] 当前 AstrBot 不支持 register_web_api，面板不可用", PLUGIN_ID)

    # ================= 配置与校验 =================

    def _cfg_projects(self) -> list:
        v = self.config.get("projects")
        return v if isinstance(v, list) else []

    def _find_project(self, pid: str) -> dict | None:
        for p in self._cfg_projects():
            if isinstance(p, dict) and p.get("id") == pid:
                return p
        return None

    async def _save(self) -> None:
        try:
            if hasattr(self.config, "save_config_async"):
                await self.config.save_config_async()
            elif hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.warning("[%s] 配置保存失败: %s", PLUGIN_ID, e)
            raise

    @staticmethod
    def _norm_rel(rel: str) -> str:
        return str(rel or "").replace("\\", "/").strip("/")

    def _safe_file(self, project: dict, rel: str) -> str:
        """把相对路径解析为项目内绝对路径；越界/非 .py 抛 ValueError。"""
        rel = self._norm_rel(rel)
        if not rel or ".." in rel.split("/"):
            raise ValueError("非法文件路径")
        if not rel.endswith(".py"):
            raise ValueError("只能操作 .py 脚本文件")
        root = os.path.realpath(project["path"])
        abs_path = os.path.realpath(os.path.join(root, rel))
        try:
            if os.path.commonpath([root, abs_path]) != root:
                raise ValueError("文件路径越出项目目录")
        except ValueError:
            raise ValueError("文件路径越出项目目录")
        return abs_path

    def _console(self, pid: str) -> ProjectConsole:
        return self.consoles.setdefault(pid, ProjectConsole(pid))

    # ================= 生命周期 =================

    async def initialize(self) -> None:
        for proj in self._cfg_projects():
            if not (isinstance(proj, dict) and proj.get("id")):
                continue
            files = proj.get("autoRun")
            if not (isinstance(files, list) and files):
                continue
            if not os.path.isdir(str(proj.get("path", ""))):
                continue
            for rel in files:
                try:
                    await self._start_file(proj, rel)
                    logger.info("[%s] 自动运行 %s :: %s", PLUGIN_ID, proj.get("name"), rel)
                except Exception as e:
                    logger.warning("[%s] 自动运行失败 %s: %s", PLUGIN_ID, rel, e)

    async def terminate(self) -> None:
        for proj in self._cfg_projects():
            if isinstance(proj, dict) and proj.get("id"):
                await self._stop_all(proj["id"], timeout_each=8)

    # ================= 进程管理 =================

    async def _start_file(self, project: dict, rel: str) -> str:
        rel = self._norm_rel(rel)
        abs_path = self._safe_file(project, rel)
        if not os.path.isfile(abs_path):
            raise ValueError(f"文件不存在: {rel}")
        pid = project["id"]
        con = self._console(pid)
        async with con.lock:
            for p in con.procs.values():
                if p.file == rel:
                    raise ValueError(f"脚本「{rel}」已在运行中 (pid={p.proc.pid})")
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            kwargs = dict(
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.realpath(project["path"]),
                env=env,
            )
            if os.name != "nt":
                kwargs["start_new_session"] = True
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", abs_path, **kwargs)
            except FileNotFoundError:
                raise ValueError(f"找不到 Python 解释器: {sys.executable}")
            run_id = uuid.uuid4().hex[:8]
            handle = ProcHandle(run_id, rel, proc)
            con.procs[run_id] = handle
            con.append(os.path.basename(rel), f"[面板] 已启动 (pid={proc.pid})")
            handle.reader = asyncio.create_task(self._read_loop(pid, handle))
            handle.waiter = asyncio.create_task(self._wait_loop(pid, handle))
            return run_id

    async def _read_loop(self, pid: str, handle: ProcHandle) -> None:
        con = self._console(pid)
        tag = os.path.basename(handle.file)
        proc = handle.proc
        try:
            while proc.stdout:
                line = await proc.stdout.readline()
                if not line:
                    break
                con.append(tag, line.decode("utf-8", "replace").rstrip("\r\n"))
        except Exception as e:
            con.append(tag, f"[面板] 读取输出异常: {e}")
        finally:
            try:
                if proc.stdout:
                    rest = await proc.stdout.read()
                    if rest:
                        for part in rest.decode("utf-8", "replace").split("\n"):
                            if part:
                                con.append(tag, part.rstrip("\r"))
            except Exception:
                pass

    async def _wait_loop(self, pid: str, handle: ProcHandle) -> None:
        con = self._console(pid)
        tag = os.path.basename(handle.file)
        try:
            code = await handle.proc.wait()
        except Exception:
            code = -1
        con.procs.pop(handle.run_id, None)
        con.append(tag, f"[面板] 进程已退出，返回码 {code}")

    async def _kill_proc(self, pid: str, handle: ProcHandle) -> None:
        proc = handle.proc
        if proc.returncode is not None:
            return
        tag = os.path.basename(handle.file)
        self._console(pid).append(tag, "[面板] 正在停止...")
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(killer.wait(), timeout=15)
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except asyncio.TimeoutError:
            proc.kill()
        except Exception as e:
            self._console(pid).append(tag, f"[面板] 停止异常: {e}")
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass

    async def _stop_one(self, pid: str, run_id: str) -> None:
        con = self._console(pid)
        handle = con.procs.get(run_id)
        if not handle:
            raise ValueError("进程不存在或已退出")
        async with con.lock:
            handle = con.procs.get(run_id)
            if not handle:
                raise ValueError("进程不存在或已退出")
            await self._kill_proc(pid, handle)
            con.procs.pop(run_id, None)

    async def _stop_all(self, pid: str, timeout_each: float = 10) -> None:
        con = self.consoles.get(pid)
        if not con:
            return
        async with con.lock:
            handles = list(con.procs.values())
        for h in handles:
            try:
                await asyncio.wait_for(self._kill_proc(pid, h), timeout=timeout_each + 15)
            except Exception:
                pass
            con.procs.pop(h.run_id, None)

    # ================= 面板 API =================

    async def _body(self):
        payload = await request.json(default=None)
        return payload if isinstance(payload, dict) else None

    async def web_projects(self):
        items = []
        for p in self._cfg_projects():
            if not isinstance(p, dict):
                continue
            path = str(p.get("path", ""))
            con = self.consoles.get(p.get("id"))
            items.append({
                "id": p.get("id"),
                "name": p.get("name", ""),
                "path": path,
                "autoRun": list(p.get("autoRun") or []),
                "valid": os.path.isdir(path),
                "status": con.status() if con else {
                    "running": 0, "procs": [], "lines": 0, "cursor": 0},
            })
        return json_response({"ok": True, "projects": items, "version": VERSION})

    async def web_save_project(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        raw = body.get("project")
        if not isinstance(raw, dict):
            return error_response("project 字段必须是对象")
        pid = raw.get("id")
        keep = None
        if isinstance(pid, str) and ID_RE.match(pid):
            keep = self._find_project(pid)
            if not keep:
                return error_response("项目不存在（可能刚被删除）")
        sid = keep["id"] if keep else uuid.uuid4().hex[:12]
        name = str(raw.get("name") or "").strip()
        if not name:
            return error_response("项目名称不能为空")
        if len(name) > MAX_NAME:
            return error_response(f"项目名称不能超过 {MAX_NAME} 个字符")
        path = str(raw.get("path") or "").strip().strip('"')
        if not path:
            return error_response("项目路径不能为空")
        if not os.path.isdir(path):
            return error_response(f"路径不存在或不是文件夹: {path}")
        auto = raw.get("autoRun", keep.get("autoRun", []) if keep else [])
        if not isinstance(auto, list):
            return error_response("autoRun 必须是列表")
        try:
            for rel in auto:
                self._safe_file({"path": path}, rel)  # 仅校验合法性
        except ValueError as e:
            return error_response(f"自动运行清单非法: {e}")
        for p in self._cfg_projects():
            if isinstance(p, dict) and p.get("id") != sid and str(p.get("path", "")) == path:
                return error_response(f"该路径已被项目「{p.get('name')}」添加")
        cleaned = {
            "id": sid, "name": name, "path": os.path.realpath(path),
            "autoRun": [self._norm_rel(r) for r in auto],
            "createdAt": keep.get("createdAt", time.time()) if keep else time.time(),
        }
        projects = self._cfg_projects()
        for i, p in enumerate(projects):
            if isinstance(p, dict) and p.get("id") == sid:
                projects[i] = cleaned
                break
        else:
            projects.append(cleaned)
        self.config["projects"] = projects
        try:
            await self._save()
        except Exception as e:
            return error_response(f"保存失败: {e}")
        return json_response({"ok": True, "id": sid})

    async def web_delete_project(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        pid = body.get("id")
        if not (isinstance(pid, str) and ID_RE.match(pid)):
            return error_response("缺少合法的项目 id")
        if not self._find_project(pid):
            return error_response("项目不存在")
        await self._stop_all(pid)
        self.consoles.pop(pid, None)
        self.config["projects"] = [
            p for p in self._cfg_projects()
            if not (isinstance(p, dict) and p.get("id") == pid)]
        try:
            await self._save()
        except Exception as e:
            return error_response(f"保存失败: {e}")
        # 注意：只移除登记，不删除用户文件夹
        return json_response({"ok": True})

    async def web_browse(self):
        body = await self._body() or {}
        path = body.get("path")
        if not isinstance(path, str) or not path:
            if os.name == "nt":
                dirs = [{"name": f"{c}:\\", "path": f"{c}:\\"}
                        for c in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                        if os.path.exists(f"{c}:\\")]
                return json_response({"ok": True, "at": "", "parent": None, "dirs": dirs})
            path = "/"
        path = path.rstrip("\\/") or path
        if not os.path.isdir(path):
            return error_response(f"目录不存在: {path}")
        dirs = []
        try:
            for e in os.scandir(path):
                try:
                    if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "$")) \
                            and e.name.lower() not in SKIP_DIRS:
                        dirs.append({"name": e.name, "path": e.path})
                except OSError:
                    continue
        except PermissionError:
            return error_response(f"无权限访问: {path}")
        dirs.sort(key=lambda d: d["name"].lower())
        parent = os.path.dirname(path)
        if parent == path:
            parent = None
        return json_response({"ok": True, "at": path, "parent": parent,
                              "dirs": dirs[:MAX_BROWSE_DIRS]})

    async def _require_project(self, body):
        pid = body.get("id")
        proj = self._find_project(pid) if isinstance(pid, str) else None
        if not proj:
            raise ValueError("项目不存在")
        if not os.path.isdir(proj["path"]):
            raise ValueError(f"项目路径已失效: {proj['path']}")
        return proj

    async def web_files(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        try:
            proj = await self._require_project(body)
        except ValueError as e:
            return error_response(str(e))
        root = os.path.realpath(proj["path"])
        files = []
        truncated = False

        def walk(d, depth):
            nonlocal truncated
            try:
                entries = sorted(os.scandir(d), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            except (PermissionError, OSError):
                return
            for e in entries:
                if len(files) >= MAX_TREE_FILES:
                    truncated = True
                    return
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name not in SKIP_DIRS and depth < MAX_TREE_DEPTH:
                            walk(e.path, depth + 1)
                    elif e.is_file(follow_symlinks=False) and e.name.endswith(".py"):
                        files.append(self._norm_rel(os.path.relpath(e.path, root)))
                except OSError:
                    continue

        walk(root, 0)
        return json_response({"ok": True, "files": files, "truncated": truncated})

    async def web_read_file(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        try:
            proj = await self._require_project(body)
            abs_path = self._safe_file(proj, body.get("file"))
        except ValueError as e:
            return error_response(str(e))
        if not os.path.isfile(abs_path):
            return error_response(f"文件不存在: {body.get('file')}")
        try:
            size = os.path.getsize(abs_path)
            if size > MAX_FILE_BYTES:
                return error_response(f"文件过大({size}B)，上限 {MAX_FILE_BYTES}B")
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return json_response({"ok": True, "content": content, "mtime": os.path.getmtime(abs_path)})
        except Exception as e:
            return error_response(f"读取失败: {e}")

    async def web_write_file(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        try:
            proj = await self._require_project(body)
            abs_path = self._safe_file(proj, body.get("file"))
        except ValueError as e:
            return error_response(str(e))
        content = body.get("content")
        if not isinstance(content, str):
            return error_response("content 必须是字符串")
        if len(content.encode("utf-8", "replace")) > MAX_FILE_BYTES:
            return error_response(f"内容过大，上限 {MAX_FILE_BYTES}B")
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            return json_response({"ok": True})
        except Exception as e:
            return error_response(f"保存失败: {e}")

    async def web_run(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        try:
            proj = await self._require_project(body)
            run_id = await self._start_file(proj, body.get("file"))
        except ValueError as e:
            return error_response(str(e))
        except Exception as e:
            logger.exception("[%s] 运行失败", PLUGIN_ID)
            return error_response(f"运行失败: {e}")
        return json_response({"ok": True, "runId": run_id})

    async def web_stop(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        try:
            proj = await self._require_project(body)
        except ValueError as e:
            return error_response(str(e))
        run_id = body.get("runId")
        try:
            if isinstance(run_id, str) and run_id:
                await self._stop_one(proj["id"], run_id)
            else:
                await self._stop_all(proj["id"])
        except ValueError as e:
            return error_response(str(e))
        return json_response({"ok": True})

    async def web_logs(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        pid = body.get("id")
        con = self.consoles.get(pid) if isinstance(pid, str) else None
        empty_status = {"running": 0, "procs": [], "lines": 0, "cursor": 0}
        if not con:
            return json_response({"ok": True, "lines": [], "next": 0, "reset": False,
                                  "status": empty_status})
        try:
            offset = int(body.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        start_seq = con.lines[0]["seq"] if con.lines else con.seq + 1
        reset = offset != 0 and offset < start_seq - 1
        if reset:
            offset = 0
        lines = [ln for ln in con.lines if ln["seq"] > offset]
        nxt = lines[-1]["seq"] if lines else min(offset, con.seq)
        return json_response({"ok": True, "lines": lines, "next": nxt,
                              "reset": reset, "status": con.status()})

    async def web_clear_log(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        pid = body.get("id")
        con = self.consoles.get(pid) if isinstance(pid, str) else None
        if con:
            con.lines.clear()
        return json_response({"ok": True})
