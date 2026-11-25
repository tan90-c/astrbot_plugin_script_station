# astrbot_plugin_script_station（脚本挂机面板）

AstrBot WebUI 内的**项目文件夹式** Python 脚本挂机面板，交互风格类似 VSCode + 群变量指令面板的侧栏标签页。

## 功能

1. **默认目录**：文件浏览与新建项目默认在 `data/plugin_data/astrbot_plugin_script_station/` 下，方便统一管理（浏览对话框可一键跳回默认目录或全盘盘符，也可选本机任意文件夹）。
2. **添加项目文件夹**：首页填路径或用「浏览…」选择器；**路径留空 = 按项目名在默认目录自动创建文件夹**；浏览框内可直接「＋建文件夹」。
3. **文件树**：项目标签页左侧展示目录内**全部文件**（自动跳过 `__pycache__`、`.git`、`venv`、`node_modules` 等），按类型显示图标：
   - 🐍 `.py`：可编辑、可运行/停止、可设自动运行
   - 📄/⚙️ 文本类（json/yaml/md/txt/html/js…）：可编辑保存（Ctrl+S）、可删除，不可运行
   - 🖼 图片（png/jpg/gif/webp/bmp/ico）：预览（base64 内嵌，≤5MB）
   - 📦 二进制：只展示大小信息
   - 所有文件支持删除（真删磁盘文件，二次确认）；读写沙箱在项目根内
4. **每项目一个共享控制台**：该项目所有运行脚本的 stdout/stderr 汇入同一控制台，行首带 `[文件名]` 前缀与时间戳，1.2s 增量轮询、自动滚动、清屏，保留最近 3000 行。
5. **运行/停止常驻脚本**：`python -u 文件` 子进程方式运行，`app.run(host=..., port=...)`、长循环、定时任务等不阻塞 AstrBot。停止时 Windows 用 `taskkill /F /T` 杀整个进程树（兼容 reloader 子进程），Linux/macOS 用进程组 SIGTERM→SIGKILL。支持同项目并行多个脚本，可单独停或「停止全部」。
6. **自动运行**：文件编辑器里「设为自动运行」，插件加载（AstrBot 启动/重载）时自动拉起这些脚本，实现开机挂机。
7. **安全**：删除项目只解除登记，**绝不删除你的文件夹**；文件读写被沙箱在项目根目录内（`..` 越界拒绝，仅允许 `.py`）；单文件读写上限 200KB。

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
| `browse` | POST | `{path?}` 目录浏览：缺省=默认目录，`""`=盘符 | 
| `mkdir` | POST | `{path?,name}` 新建子文件夹（缺省父=默认目录） |
| `files` | POST | `{id}` 项目内全部文件树（≤2000 个，深度≤6） |
| `read_file` / `write_file` | POST | `{id,file[,content]}` 文本读写（任意类型，防越界；二进制返回 binary 标记） |
| `delete_file` | POST | `{id,file}` 删除单个文件（真删，仅限项目内普通文件） |
| `preview` | POST | `{id,file}` 图片预览，返回 `{dataUrl}` base64 |
| `run` | POST | `{id,file}` 运行脚本文件 |
| `stop` | POST | `{id[,runId]}` 停止单个/全部 |
| `logs` | POST | `{id,offset}` 项目控制台增量输出 |
| `clear_log` | POST | `{id}` 清空控制台 |

## v2.2.1 变更

- 修复：dll 等大二进制/超大文件无法选中。所有类型文件均可选中：二进制只报信息、超大文本提示不支持编辑，但仍可删除。

## v2.2 变更

- 文件树展示项目内全部文件（不再只列 .py）。
- 非 .py 文本文件可编辑/保存/删除；图片可预览；二进制文件展示信息；只有 .py 可运行/自动运行；新增 `delete_file`、`preview` 接口。

## v2.1 变更

- 文件浏览/新建文件夹/留空路径创建项目，默认基准目录为 `data/plugin_data/astrbot_plugin_script_station`（自动创建）。

## v2 变更说明

- v1 的「脚本库存配置」模型已替换为「项目文件夹 + 真实文件」模型；配置键由 `scripts` 变为 `projects`（v1 数据不迁移，请在面板重新添加项目并把脚本保存为文件）。
- v2 采用左侧栏标签页 + 右侧内容区的面板布局。

## 注意

- 面板可浏览本机目录、读写项目内 .py 并执行任意脚本，能打开 AstrBot WebUI 即等同本机权限，**请勿把 WebUI 暴露公网**。
