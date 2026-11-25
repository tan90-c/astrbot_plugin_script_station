# astrbot_plugin_script_station（脚本挂机面板）

在 AstrBot WebUI 里提供可视化面板，管理并**常驻挂机**任意 Python 脚本。

## 功能

1. 首页「添加脚本」：填名称 + 粘贴 Python 代码即可创建，自动为其生成标签页。
2. 每个脚本一个标签页：内置代码编辑器（保存后生效）与 ▶ 启动 / ■ 停止 按钮。
3. 实时控制台：标签页下半屏显示脚本的 stdout/stderr 合并输出（1.2s 增量轮询、自动滚动、清屏），
   保留最近 3000 行。
4. 常驻脚本支持：脚本以 `python -u main.py` 子进程运行，`app.run(host=..., port=...)`、
   长循环、定时任务等不会阻塞 AstrBot；停止时 Windows 用 `taskkill /F /T` 杀整个进程树
   （兼容 Flask/uvicorn 的 reloader 子进程），Linux/macOS 用进程组 SIGTERM→SIGKILL。
5. 自动启动：勾选「加载时自动启动」的脚本会在插件加载（AstrBot 启动/插件重载）后自动拉起，
   实现开机挂机。
6. 脚本持久化：代码存放在插件配置 `scripts` 键内（已写入 `_conf_schema.json`，
   重载/更新不会丢）。

## 安装

1. AstrBot 插件市场搜索 `astrbot_plugin_script_station`，或把本目录放到 `data/plugins/`。
2. 重启/刷新插件后，进入 WebUI「插件 → 脚本挂机面板」打开控制面板。
3. 注意：脚本使用 **AstrBot 自身的 Python 解释器** 运行（`sys.executable`），
   所需依赖（如 flask）需提前装进 AstrBot 的 Python 环境。

## 示例：常驻 Flask 服务

```python
from flask import Flask
app = Flask(__name__)

@app.route("/")
def index():
    return "hello from script station"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
```

启动后在控制台可见 Werkzeug 日志；点「停止」即杀进程树释放端口。

## 面板 API（供二次开发）

前缀 `/api/v1/plugins/extensions/astrbot_plugin_script_station/`：

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `scripts` | GET | 脚本全量列表（含运行状态） |
| `scripts/save` | POST | `{script:{id?,name,code,autoStart}}` 新增/更新 |
| `scripts/delete` | POST | `{id}` 删除（自动停止并清目录） |
| `run` / `stop` | POST | `{id}` 启动/停止 |
| `logs` | POST | `{id,offset}` 增量日志 `{lines,next,reset,status}` |
| `clear_log` | POST | `{id}` 清空日志缓冲 |

## 限制与说明

- 单脚本代码上限 200KB；脚本运行目录为插件目录下 `run/<id>/`（删除脚本时自动清理）。
- 面板运行在 iframe 中，确认框为页面内自绘模态，不使用 `window.confirm`。
- 无鉴权扩展：能打开 AstrBot WebUI 即可操作脚本，请勿把 WebUI 暴露到公网。
