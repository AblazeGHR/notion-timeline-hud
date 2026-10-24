# 时间线任务面板 · 规格 v0.1

## 1. 产品形态

- Windows 常驻小应用，开机自启，默认隐藏。
- 全局快捷键（默认 `Ctrl+Alt+Space`）唤出；半透明、无边框、置顶；失焦自动隐藏（可配）。
- 窗口约 460 × 720，屏幕右侧居中弹出。
- 核心只有一个视图：**多轨时间线**。外加一个小的**设置弹窗**（快捷键、透明度、开机自启、刷新间隔、隐藏后是否释放内存），以及一个**托盘图标**（显示/隐藏、刷新数据、开机自启、退出）。
- 点击条目跳转到对应 Notion 页面。

## 2. 数据源事实（2026-10-24 本机实测）

- 本地缓存：`%APPDATA%\Notion\notion.db`（SQLite，**只读**，桌面端运行时秒级同步，关闭后冻结）。
- 目标库：「日程与任务数据库」，`collection_id = 31c55e1d-4f6d-80a2-8f74-000b970d5404`。
- **这个 id 是 data source id，不是 database id**：`GET /v1/databases/{id}` 会 404，必须走
  `GET /v1/data_sources/{id}` 与 `POST /v1/data_sources/{id}/query`。写页面时 `parent` 用
  `{"type": "data_source_id", "data_source_id": ...}`。
- 2026-10-24 实测：API 返回 54 行，与本地缓存 id 完全一致；但 **API 的 status 全部有值**
  （54/54「未开始」），缓存里只有 36 行有值 —— 差的那 18 行是 Notion 的默认状态没落盘。
  所以展示以 API 为准，缓存只负责秒开与离线。
- 行存储：`block` 表，`parent_table='collection'`，`parent_id=<collection_id>`，`type='page'`，`alive=1`。
- 属性在 `block.properties` 里是 JSON，键是**属性短 ID**；`collection.schema` 把短 ID 映射到 `{name, type}`。
- 富文本形如 `[["文字", [["h","b"]]]]`；日期形如 `[["‣",[["d",{"start_date":"2026-10-22","end_date":null,"time_zone":null}]]]]`。
- 实测 54 行；属性分布：名称 54、状态 36、截止日期 34、安排日期 16、大方向 44、学科 12、Tag 2。
- **写入必须走 Notion REST API**；本地缓存不可写（直写不会上行，且会被同步覆盖）。
- Notion API 不支持永久删除：`DELETE /v1/blocks/{id}` 与 `PATCH /v1/pages/{id} {"archived":true}` 都只是移入回收站。UI 文案写「删除」，语义是移入回收站。

## 3. 统一 Task 模型

```json
{
  "id": "3e355e1d-4f6d-815c-99f1-ef8b5ff117fa",
  "source": "notion",
  "title": "离散数学 Assignment 2",
  "url": "https://www.notion.so/3e355e1d4f6d815c99f1ef8b5ff117fa",
  "created_time": "2026-10-20T09:12:00.000Z",
  "last_edited_time": "2026-10-24T10:07:00.000Z",
  "dates": {
    "安排日期": {"start": "2026-10-22", "end": null},
    "截止日期": {"start": "2026-10-25", "end": null}
  },
  "status": {"name": "未开始", "color": "default"},
  "properties": {
    "大方向": {"type": "select", "value": "学习"},
    "学科": {"type": "select", "value": "离散数学"},
    "Tag": {"type": "multi_select", "value": ["作业"]},
    "正在阻止": {"type": "relation", "value": ["<page_id>"]}
  }
}
```

要求：**全套镜像**。所有属性都要带上，不得只取标题/日期/状态。

## 4. 多轨时间线

- 每个 `date` 类型属性 = 一条轨道，轨道顺序可配置（默认 安排日期 → 截止日期）。
- 时间轴**垂直**，刻度按天。视口中心 = 「现在」：一条 now 线固定在垂直中线，内容随真实时间流动。
- 可向前/向后滚动翻查；默认加载 ±14 天，滚到边缘再懒加载。
- 同一任务有多个日期时，在每条对应轨道上各出现一次，共享同一个 id。
- 无日期任务收进底部「待安排」抽屉（可展开），不占轨道。
- 轨道内按时间排序，同一天内按状态/标题稳定排序。

## 5. 交互

- 单击标题文字 = 打开 Notion 页面；单击条目其他区域 = 选中。
- 双击标题 = 就地编辑。
- 上线拖动 = 改该轨道对应日期；拖动时吸附到天，显示日期气泡。
- 点击状态圆点 = 循环切换状态。
- 右键 = 菜单：改状态 / 改日期 / 移动轨道 / 删除 / 复制链接。
- 顶部快速新增：`明天 15:00 交作业` → 解析时间 + 标题，回车即写入。
- 写操作乐观更新，失败回滚并提示；离线时进重试队列。

## 6. 视觉（mock 重点）

- 深色毛玻璃：`rgba(18,18,20,0.72)` + `backdrop-filter: blur(24px)`，圆角 18px，1px 描边 `rgba(255,255,255,0.08)`，大阴影。
- 主色 `#7C8CFF`；轨道色：安排 `#7C8CFF`、截止 `#FF6B8A`，其余轨道自动分配。
- 状态色：未开始 `#6B7280`、进行中 `#F5A524`、已完成 `#30C48D`、空 `#3F4550`。
- 字体 `system-ui, "Microsoft YaHei"`；数字用等宽 `tabular-nums`。
- 动效：唤出 150ms 上浮 + 淡入；hover 条目抬 1px；now 线带柔光。
- 适配 125% 缩放（不要用小于 12px 的字号）。

## 7. 技术约束

- Python 3.12，解释器固定 `E:/software/miniforge/python.exe`。
- 读取：`sqlite3` 标准库（先复制 db 到临时文件再读，避开写锁）。写入：`httpx`。
- Token 从 `.env` 的 `NOTION_TOKEN` 读，禁止硬编码、禁止提交。
- UI 用 pywebview + WebView2（本机已装），等 mock 定稿后再接。
- 不写本地 Notion 缓存。

## 8. 目录约定

```
docs/SPEC.md          本文件
mock/                 前端外观稿（单文件 HTML，双击可开）
src/notion_timeline/  后端：adapter + 数据模型 + CLI
```
