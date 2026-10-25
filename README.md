# Timeline HUD

Windows 上的常驻时间线面板：把 Notion「日程与任务数据库」画成一条可拖拽的多轨时间线，
全局热键唤出、半透明置顶、失焦可自动收起。

- 数据：读取走 Notion REST API，失败回落到 Notion 桌面端的本地 SQLite 缓存（只读）。
- 写入：全部走 API（缓存不可写）。
- 界面：无边框 + DWM 亚克力（Win11），单文件 HTML，WebView2 渲染。

## 运行

```bash
# 正常启动（显示窗口，Ctrl+Alt+Space 唤出/收起）
E:/software/miniforge/python.exe app/main.py

# 开发用：不因失焦自动隐藏（配合热键调试）
E:/software/miniforge/python.exe app/main.py --keep-open

# 开机自启用：创建窗口但不显示，只有热键/再次启动才唤出
E:/software/miniforge/python.exe app/main.py --hidden

# 关掉亚克力（排障用：纯透明而不是模糊）
E:/software/miniforge/python.exe app/main.py --no-blur

# 命令行开关开机自启
E:/software/miniforge/python.exe app/main.py --autostart on|off
```

依赖：Python 3.11+、`pywebview`（WebView2 运行时）、`httpx`、`python-dotenv`、
`pystray` + `Pillow`（托盘图标；缺失时只记日志，主程序照常运行）。
Notion token 放在环境变量 `NOTION_TOKEN` 或仓库根 `.env`（`.env` 已被 gitignore）。
数据源 id 必须配在 `NOTION_DATA_SOURCE_ID`（环境变量或 `.env`）；缺失会直接报
「缺少 NOTION_DATA_SOURCE_ID，请配置环境变量或 .env」，不会猜默认库。

## 热键

| 组合 | 作用 |
|---|---|
| `Ctrl+Alt+Space`（默认，可在设置里改） | 唤出 / 收起窗口 |
| `Ctrl+Alt+Q` | 退出进程 |

热键改了之后若被别的程序占用，`set_config` 会失败并把设置回滚。

## 托盘

窗口旁边还有一个小托盘图标（深色圆角底 + 一道竖向时间轴 + 当前时刻亮点，与面板同色系）：

| 操作 | 作用 |
|---|---|
| 双击图标 | 显示 / 隐藏（等同热键） |
| 菜单「显示 / 隐藏」 | 同上，文案跟着当前状态变 |
| 菜单「刷新数据」 | 让前端重新拉一次 Notion 数据（等价点面板上的 ↻） |
| 菜单「开机自启」 | 勾选状态取自注册表；点击即写入 / 删除 `HKCU\...\Run` |
| 菜单「退出」 | 停掉托盘并退出进程 |

托盘用 `pystray` + `Pillow` 实现（`app/tray.py`）。这两个包没装、或托盘创建失败时，
启动日志会打一行说明，程序继续无托盘运行，热键和面板功能不受影响。

## 设置项

设置浮层（右上齿轮）里可调：唤出快捷键（点击捕获，需带修饰键）、面板透明度、
时间轴密度（6~48 px/h）、在 Notion 应用中打开、失焦自动隐藏（默认关）、
开机自启（写 `HKCU\...\Run`，`pythonw` 启动，命令带 `--hidden`）、退出应用。

配置落在 `app/config.json`（gitignore），字段：`hotkey / opacity / timeline_scale /
font_scale / hide_on_blur / release_memory_minutes / refresh_seconds / open_in_app /
seen_intro / autostart`。

## 目录

```
app/
  main.py           桌面壳：窗口、热键、单实例、亚克力/透明、开机自启、后台刷新、
                    正文并发读取与 hover 预取、JS 桥
  data.py           数据适配：API 优先、缓存回落、Task → UI 形状
  config.py         config.json 读写、校验、原子写入
  quickadd.py       「明天 15:00 写周报」这类自然语言解析
  tray.py           系统托盘：图标、菜单、显示/隐藏、刷新、自启、退出
  ui/index.html     全部界面（HTML + CSS + JS，单文件）
src/notion_timeline/
  api_client.py     Notion REST 客户端（data source 查询、页面/块读写）
  cache_reader.py   本地 SQLite 缓存只读解析
  models.py         Task / DateValue / PropertyValue
  dates.py          shift()：按分钟/整天平移日期（含 end）
  cli.py            命令行入口
docs/               SPEC.md（数据事实与设计）、TEST-UI.md（人工验收清单）
tests/              test_config.py / test_dates.py（纯 assert，可直接跑）
spike/              一次性探针：截图、静态渲染、窗口试验
mock/               外观稿（浏览器可直接打开）
```

## 测试

```bash
# 单元
E:/software/miniforge/python.exe tests/test_config.py
E:/software/miniforge/python.exe tests/test_dates.py

# 浏览器内自测入口（无窗口打开 app/ui/index.html 时可用）
window.__hud.getScale() / setScale(v) / getFontScale() / setFontScale(v) /
resetScale() / setData(data) / setStatus({source}) / markIntroSeen() / edgeSpeed(y) /
refresh()
```

人工验收清单见 [docs/TEST-UI.md](docs/TEST-UI.md)。

## 已知限制

- **正文是纯文本**：详情弹窗的「内容」页只渲染 `plain_text`，加粗/斜体/颜色/链接/行内代码不显示；
  就地编辑会把该块 rich_text 合并成一个 run，原有行内格式会丢。
- **删除是软删除**：Notion 没有永久删除，`DELETE /v1/blocks/{id}` 只是移入回收站，30 天内可恢复。
- **内容缓存 60 秒**：`get_page_content` 按页缓存 60s，在 Notion 里改了正文要等过期或点内容页的 ↻。
- **窗口不是全透**：WinForms 的 `#F0F0F0` 底色曾挡住合成层，现由 `enable_transparency()`
  改 Form 底色 + DWM 系统背景解决；但若系统关了「透明效果」，会退回不透明深色。
- **单实例**：靠 `Global\TimelineHUD_v1` 互斥体，第二个进程通过命名管道唤出首个实例后退出 0。
- **多屏**：唤出时按鼠标所在显示器的右缘全屏高定位；首次创建仍用主屏。
- **自启路径**：写的是当前 `ROOT/main.py` 的绝对路径 + `pythonw.exe`，移动仓库目录后需要重开关一次。
- **状态选项名随库不同**：正式库是「完成」，测试库是「已完成」，写入前会读 data source 选项做映射。
