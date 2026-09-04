# 前端布局优化：规则概述化 · 后台异步 · 历史任务

日期：2026-08-11
范围：投标文件合规审核工具（biddingfiles-Review）前端 + 后端任务系统

## 一、本次改动三项需求

| 需求 | 实现 |
| --- | --- |
| 规则清单改为概述样式、置于上传区下方、可点击选择/取消 | 重构 `RulePanel` 为「审核规则清单」概述卡片，按类别分组，每条规则是可点击 chip，支持整组/全选/清空 |
| 审核任务改为后台异步执行，不阻塞界面 | 后端新增任务存储与任务接口，提交即返回 task_id；前端 1.5s 轮询进度，开始按钮不再禁用界面 |
| 历史任务列表支持查看详情（状态/时间/结果等） | 新增 `HistoryPanel`，列表展示状态、创建/结束时间、进度、文件、规则数；点击打开详情抽屉（状态、时间、得分、逐条结论、过程日志） |

## 二、后端改动

- `backend/services/task_store.py`（新增）：内存任务存储，记录请求快照、实时进度、过程日志、结论/一致性问题/知识库轨迹/汇总，并通过 `asyncio.Event` 支持取消。
- `backend/routers/review.py`（扩展）：新增
  - `POST /api/review/tasks`：校验文件/规则后立即返回 `{task_id}`，`asyncio.create_task` 后台驱动 `review_engine.run_review` 并持续写入任务存储（不阻塞接口）。
  - `GET /api/review/tasks` 历史列表（最新在前）
  - `GET /api/review/tasks/{id}` 任务详情
  - `POST /api/review/tasks/{id}/cancel` 取消
  - `DELETE /api/review/tasks/{id}` 删除单条 / `DELETE /api/review/tasks` 清空
  - 原有 `POST /api/review/stream`（SSE）保留兼容。

## 三、前端改动

- `types/index.ts`：新增 `TaskStatus / ReviewTaskSummary / ReviewTaskDetail / LogEntry`。
- `services/api.ts`：新增 `createReviewTask / listTasks / getTask / cancelTask / deleteTask / clearTasks`。
- `hooks/useReviewTasks.ts`（新增）：管理任务列表、活动任务、轮询刷新、生命周期操作。旧 `hooks/useReview.ts` 已删除，`LogEntry` 迁移至 `types`。
- `components/RulePanel.tsx`：概述化可点击规则卡片。
- `components/HistoryPanel.tsx`（新增）：历史列表 + 详情抽屉（复用 `ProgressPanel`/`ResultPanel`）。
- `components/ProgressPanel.tsx`：`LogEntry` 引用迁移至 `types`。
- `App.tsx`：左栏（文件 + 规则概述 + 选项）/ 右栏 Tabs（当前任务 / 历史任务），开始审核走后台任务、非阻塞。
- `index.css`：规则 chip 与历史列表项样式。

## 四、验证

- `npm run build`：`tsc -b && vite build` 通过。
- Docker 重建并重启（`docker compose up -d --build`），容器 `healthy` / `Up`，nginx 反代 `/api` 通畅。
- 接口链路实测（容器内）：create 立即返回 task_id → 历史列表出现 `running`（且 `progress_message` 显示正在审规则，证明容器能真实连到外部千问 LLM）→ cancel 变 `cancelled` → delete / clear 均成功。
- 说明：任务存储为内存态，容器重启后历史清空（与文件存储一致，属预期）。

## 五、访问

- 前端：http://localhost:8080  （效果等同本地 5173）
- 后端健康检查：http://localhost:8100/api/health
- 历史任务接口：http://localhost:8100/api/review/tasks
