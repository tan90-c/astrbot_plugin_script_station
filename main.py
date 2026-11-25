# -*- coding: utf-8 -*-
"""astrbot_plugin_script_station — 脚本挂机面板

可视化面板管理用户 Python 脚本：
1. 首页添加/编辑脚本；
2. 每个脚本一个标签页，可启动/停止；
3. 标签页内实时查看控制台输出（stdout+stderr 合并）；
4. 支持 app.run(host=..., port=...) 这类常驻脚本（子进程持续运行，
   面板轮询增量拉取日志，停止时杀整个进程树）。

所有 Web API 路径前缀由 AstrBot 转发为
/api/v1/plugins/extensions/astrbot_plugin_script_station/<path>。
"""
from __future__ import annotations

import asyncio
import copy
import os
import re
import shutil
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
    except ImportError:  # pragma: no cover - 极端环境，仅让 import 不炸
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
VERSION = "1.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_ROOT = os.path.join(BASE_DIR, "run")  # 派生缓存：代码以配置为准，删除脚本时清理
MAX_CODE_BYTES = 200_000
MAX_NAME = 64
MAX_LINES = 3000  # 每个脚本保留的控制台行数（环形丢弃旧行）
ID_RE = re.compile(r"^[0-9a-f]{8,32}$")


class ScriptRun:
    """单个脚本的一次运行会话：子进程 + 行缓冲 + 状态。"""

    def __init__(self, script_id: str):
        self.script_id = script_id
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task | None = None
        self.waiter: asyncio.Task | None = None
        self.lines: list[dict] = []  # [{seq, t, text}]
        self.seq = 0                 # 单调递增行号（跨 clear 不回退，便于游标）
        self.running = False
        self.stopping = False
        self.exit_code: int | None = None
        self.started_at: float = 0.0
        self.lock = asyncio.Lock()

    def append(self, text: str) -> None:
        self.seq += 1
        self.lines.append({"seq": self.seq, "t": time.time(), "text": text})
        if len(self.lines) > MAX_LINES:
            del self.lines[: len(self.lines) - MAX_LINES]

    def status(self) -> dict:
        state = "stopped"
        if self.running:
            state = "stopping" if self.stopping else "running"
        return {
            "state": state,
            "running": self.running,
            "stopping": self.stopping,
            "exitCode": self.exit_code,
            "startedAt": self.started_at,
            "pid": self.proc.pid if (self.proc and self.running) else None,
            "lines": len(self.lines),
            "cursor": self.seq,
        }


@register(PLUGIN_ID, "tan90-c", "可视化挂机/运行 Python 脚本面板", VERSION,
          "https://github.com/tan90-c/astrbot_plugin_script_station")
class ScriptStation(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self.runs: dict[str, ScriptRun] = {}
        if hasattr(context, "register_web_api"):
            context.register_web_api(f"/{PLUGIN_ID}/scripts", self.web_list, ["GET"], "脚本列表与状态")
            context.register_web_api(f"/{PLUGIN_ID}/scripts/save", self.web_save_script, ["POST"], "新增/更新脚本")
            context.register_web_api(f"/{PLUGIN_ID}/scripts/delete", self.web_delete_script, ["POST"], "删除脚本")
            context.register_web_api(f"/{PLUGIN_ID}/run", self.web_run, ["POST"], "启动脚本")
            context.register_web_api(f"/{PLUGIN_ID}/stop", self.web_stop, ["POST"], "停止脚本")
            context.register_web_api(f"/{PLUGIN_ID}/logs", self.web_logs, ["POST"], "增量拉取控制台输出")
            context.register_web_api(f"/{PLUGIN_ID}/clear_log", self.web_clear_log, ["POST"], "清空控制台缓冲")
        else:  # pragma: no cover - 旧版无面板 API
            logger.warning("[%s] 当前 AstrBot 不支持 register_web_api，面板不可用", PLUGIN_ID)

    # ---------------- 配置读写 ----------------

    def _cfg_scripts(self) -> list[dict]:
        v = self.config.get("scripts")
        return v if isinstance(v, list) else []

    async def _save(self) -> None:
        try:
            if hasattr(self.config, "save_config_async"):
                await self.config.save_config_async()
            elif hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.warning("[%s] 配置保存失败: %s", PLUGIN_ID, e)
            raise

    # ---------------- 校验 ----------------

    def _find(self, script_id: str) -> dict | None:
        for s in self._cfg_scripts():
            if s.get("id") == script_id:
                return s
        return None

    def _clean_input(self, raw, keep: dict | None) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("script 字段必须是对象")
        sid = raw.get("id")
        if not (isinstance(sid, str) and ID_RE.match(sid)):
            sid = keep["id"] if keep else uuid.uuid4().hex[:12]
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("脚本名称不能为空")
        if len(name) > MAX_NAME:
            raise ValueError(f"脚本名称不能超过 {MAX_NAME} 个字符")
        for s in self._cfg_scripts():
            if isinstance(s, dict) and s.get("id") != sid and str(s.get("name") or "").strip() == name:
                raise ValueError(f"已存在同名脚本「{name}」，请换个名称（若刚删除请刷新）")
        code = raw.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"脚本「{name}」的代码不能为空")
        if len(code.encode("utf-8", "replace")) > MAX_CODE_BYTES:
            raise ValueError(f"脚本「{name}」代码过大（上限 {MAX_CODE_BYTES // 1000}KB）")
        now = time.time()
        return {
            "id": sid,
            "name": name,
            "code": code,
            "autoStart": bool(raw.get("autoStart", keep.get("autoStart") if keep else False)),
            "createdAt": keep["createdAt"] if keep else now,
            "updatedAt": now,
        }

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        for s in self._cfg_scripts():
            if s.get("autoStart") and s.get("id"):
                try:
                    await self._start_script(s)
                    logger.info("[%s] 已自动启动脚本「%s」", PLUGIN_ID, s.get("name"))
                except Exception as e:
                    logger.warning("[%s] 自动启动「%s」失败: %s", PLUGIN_ID, s.get("name"), e)

    async def terminate(self) -> None:
        for run in list(self.runs.values()):
            if run.running:
                try:
                    await self._kill_run(run)
                except Exception:
                    pass

    # ---------------- 进程管理 ----------------

    def _run_dir(self, script_id: str) -> str:
        return os.path.join(RUN_ROOT, script_id)

    async def _start_script(self, script: dict) -> None:
        sid = script["id"]
        run = self.runs.setdefault(sid, ScriptRun(sid))
        async with run.lock:
            if run.running:
                raise ValueError(f"脚本「{script['name']}」已在运行中")
            d = self._run_dir(sid)
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "main.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(script["code"])
            # 清掉上次退出的缓冲，开始新会话
            run.lines.clear()
            run.exit_code = None
            run.stopping = False
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            kwargs = dict(
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=d,
                env=env,
            )
            if os.name != "nt":
                kwargs["start_new_session"] = True  # 便于 killpg 杀进程组
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", "main.py", **kwargs)
            except FileNotFoundError:
                raise ValueError(f"找不到 Python 解释器: {sys.executable}")
            run.proc = proc
            run.running = True
            run.started_at = time.time()
            run.append(f"[面板] 已启动 {sys.executable} -u main.py (pid={proc.pid})")
            run.reader = asyncio.create_task(self._read_loop(run))
            run.waiter = asyncio.create_task(self._wait_loop(run))

    async def _read_loop(self, run: ScriptRun) -> None:
        proc = run.proc
        try:
            while proc and proc.stdout:
                line = await proc.stdout.readline()
                if not line:
                    break
                run.append(line.decode("utf-8", "replace").rstrip("\r\n"))
        except Exception as e:  # 管道异常只记日志，不崩宿主
            run.append(f"[面板] 读取输出异常: {e}")
        finally:
            # 冲刷残留的非换行结尾输出
            try:
                if proc and proc.stdout:
                    rest = await proc.stdout.read()
                    if rest:
                        for part in rest.decode("utf-8", "replace").split("\n"):
                            if part:
                                run.append(part.rstrip("\r"))
            except Exception:
                pass

    async def _wait_loop(self, run: ScriptRun) -> None:
        try:
            code = await run.proc.wait() if run.proc else -1
        except Exception:
            code = -1
        run.running = False
        run.exit_code = code
        run.append(f"[面板] 进程已退出，返回码 {code}")

    async def _kill_run(self, run: ScriptRun) -> None:
        proc = run.proc
        if not proc or proc.returncode is not None:
            run.running = False
            return
        run.stopping = True
        run.append("[面板] 正在停止进程...")
        pid = proc.pid
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(killer.wait(), timeout=15)
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except asyncio.TimeoutError:
            proc.kill()
        except Exception as e:
            run.append(f"[面板] 停止异常: {e}")
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
        run.stopping = False

    async def stop_script(self, script_id: str) -> None:
        run = self.runs.get(script_id)
        if not run or not run.running:
            raise ValueError("脚本未在运行中")
        async with run.lock:
            await self._kill_run(run)

    # ---------------- 面板 API ----------------

    async def _body(self) -> dict | None:
        payload = await request.json(default=None)
        return payload if isinstance(payload, dict) else None

    async def web_list(self):
        items = []
        for s in self._cfg_scripts():
            if not isinstance(s, dict):
                continue
            sid = s.get("id", "")
            run = self.runs.get(sid)
            items.append({
                "id": sid,
                "name": s.get("name", ""),
                "code": s.get("code", ""),
                "autoStart": bool(s.get("autoStart")),
                "createdAt": s.get("createdAt"),
                "updatedAt": s.get("updatedAt"),
                "status": run.status() if run else {
                    "state": "stopped", "running": False, "stopping": False,
                    "exitCode": None, "startedAt": 0, "pid": None,
                    "lines": 0, "cursor": 0},
            })
        return json_response({"ok": True, "scripts": items, "version": VERSION})

    async def web_save_script(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        raw = body.get("script")
        keep = None
        if isinstance(raw, dict) and isinstance(raw.get("id"), str) and ID_RE.match(raw["id"]):
            keep = self._find(raw["id"])
        try:
            cleaned = self._clean_input(raw, keep)
        except ValueError as e:
            return error_response(str(e))
        scripts = self._cfg_scripts()
        for i, s in enumerate(scripts):
            if isinstance(s, dict) and s.get("id") == cleaned["id"]:
                scripts[i] = cleaned
                break
        else:
            scripts.append(cleaned)
        self.config["scripts"] = scripts
        try:
            await self._save()
        except Exception as e:
            return error_response(f"保存失败: {e}")
        return json_response({"ok": True, "id": cleaned["id"]})

    async def web_delete_script(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        sid = body.get("id")
        if not (isinstance(sid, str) and ID_RE.match(sid)):
            return error_response("缺少合法的脚本 id")
        if not self._find(sid):
            return error_response("脚本不存在")
        run = self.runs.pop(sid, None)
        if run:
            if run.running:
                await self._kill_run(run)
            for task in (run.reader, run.waiter):
                if task and not task.done():
                    task.cancel()
        shutil.rmtree(self._run_dir(sid), ignore_errors=True)
        self.config["scripts"] = [
            s for s in self._cfg_scripts()
            if not (isinstance(s, dict) and s.get("id") == sid)]
        try:
            await self._save()
        except Exception as e:
            return error_response(f"保存失败: {e}")
        return json_response({"ok": True})

    async def web_run(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        sid = body.get("id")
        script = self._find(sid) if isinstance(sid, str) else None
        if not script:
            return error_response("脚本不存在")
        try:
            await self._start_script(script)
        except ValueError as e:
            return error_response(str(e))
        except Exception as e:
            logger.exception("[%s] 启动脚本失败", PLUGIN_ID)
            return error_response(f"启动失败: {e}")
        return json_response({"ok": True})

    async def web_stop(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        sid = body.get("id")
        if not (isinstance(sid, str) and ID_RE.match(sid)):
            return error_response("缺少合法的脚本 id")
        try:
            await self.stop_script(sid)
        except ValueError as e:
            return error_response(str(e))
        return json_response({"ok": True})

    async def web_logs(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        sid = body.get("id")
        run = self.runs.get(sid) if isinstance(sid, str) else None
        if not run:
            return json_response({"ok": True, "lines": [], "next": 0, "status": {
                "state": "stopped", "running": False, "stopping": False,
                "exitCode": None, "startedAt": 0, "pid": None,
                "lines": 0, "cursor": 0}})
        try:
            offset = int(body.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        start_seq = run.lines[0]["seq"] if run.lines else run.seq + 1
        reset = offset < start_seq - 1 and offset != 0
        if reset:
            offset = 0
        lines = [ln for ln in run.lines if ln["seq"] > offset]
        nxt = lines[-1]["seq"] if lines else max(offset, run.seq)
        return json_response({"ok": True, "lines": lines, "next": nxt,
                              "reset": reset, "status": run.status()})

    async def web_clear_log(self):
        body = await self._body()
        if body is None:
            return error_response("请求体必须是 JSON 对象")
        sid = body.get("id")
        run = self.runs.get(sid) if isinstance(sid, str) else None
        if run:
            run.lines.clear()
        return json_response({"ok": True})
