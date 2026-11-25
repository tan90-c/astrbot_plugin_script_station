# astrbot_plugin_script_station（脚本挂机面板）

AstrBot WebUI 内的**项目文件夹式** Python 脚本挂机面板，交互风格类似 VSCode + 群变量指令面板的侧栏标签页。

## 功能

1. **添加项目文件夹**：首页填绝对路径或用「浏览…」目录选择器挑选本机文件夹，每个项目自动生成侧栏标签页。
2. **文件树**：项目标签页左侧列出目录内全部 `.py` 文件（自动跳过 `__pycache__`、`.git`、`venv`、`node_modules` 等），点击即在内置编辑器打开，可编辑保存（Ctrl+S）。
3. **每项目一个共享控制台**：该项目所有运行脚本的 stdout/stderr 汇入同一控制台，行首带 `[文件名]` 前缀与时间戳，1.2s 增量轮询、自动滚动、清屏，保留最近 3000 行。
4. **运行/停止常驻脚本**：`python -u 文件` 子进程方式运行，`app.run(host=..., port=...)`、长循环、定时任务等不阻塞 AstrBot。停止时 Windows 用 `taskkill /F /T` 杀整个进程树（兼容 reloader 子进程），Linux/macOS 用进程组 SIGTERM→SIGKILL。支持同项目并行多个脚本，可单独停或「停止全部」。
5. **自动运行**：文件编辑器里「设为自动运行」，插件加载（AstrBot 启动/重载）时自动拉起这些脚本，实现开机挂机。
6. **安全**：删除项目只解除登记，**绝不删除你的文件夹**；文件读写被沙箱在项目根目录内（`..` 越界拒绝，仅允许 `.py`）；单文件读写上限 200KB。

## 安装

1. 插件市场搜索 `astrbot_plugin_script_station`，或把本目录放到 `data/plugins/`。
2. 重启/刷新插件后，进入 WebUI「插件 → 脚本挂机面板」。
3. 脚本用 **AstrBot 自身的 Python 解释器**（`sys.executable`）运行，依赖（如 flask）需提前装进该环境。

## 示例

项目文件夹 `E:\pyprojects\demo\app.py`：

```python
from flask import Flask
app = Flask(__name__)

@app.route("/")
def index():
    return "hello from script station"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
```

文件树点开 `app.py` → ▶ 运行，控制台实时显示 Werkzeug 日志；■ 停止即杀进程树释放端口。

## 面板 API

前缀 `/api/v1/plugins/extensions/astrbot_plugin_script_station/`：

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `projects` | GET | 项目列表（含 valid / 运行进程状态） |
| `projects/save` | POST | `{project:{id?,name,path,autoRun}}` 新增/更新 |
| `projects/delete` | POST | `{id}` 删除登记（停进程，不删文件） |
| `browse` | POST | `{path?}` 本机目录浏览（盘符→逐级子目录） |
| `files` | POST | `{id}` 项目内 .py 文件树（≤800 个，深度≤6） |
| `read_file` / `write_file` | POST | `{id,file[,content]}` 项目内读写（防越界） |
| `run` | POST | `{id,file}` 运行脚本文件 |
| `stop` | POST | `{id[,runId]}` 停止单个/全部 |
| `logs` | POST | `{id,offset}` 项目控制台增量输出 |
| `clear_log` | POST | `{id}` 清空控制台 |

## v2 变更说明

- v1 的「脚本库存配置」模型已替换为「项目文件夹 + 真实文件」模型；配置键由 `scripts` 变为 `projects`（v1 数据不迁移，请在面板重新添加项目并把脚本保存为文件）。
- v2 采用左侧栏标签页 + 右侧内容区的面板布局。

## 注意

- 面板可浏览本机目录、读写项目内 .py 并执行任意脚本，能打开 AstrBot WebUI 即等同本机权限，**请勿把 WebUI 暴露公网**。
