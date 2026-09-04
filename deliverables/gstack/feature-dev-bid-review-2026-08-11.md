# 投标文件合规审核工具 · 架构分析 + 前端优化 + Docker 部署

**日期**：2026-08-11
**场景**：代码架构分析 / 前端交互优化 / 容器化上线
**参与成员（主理人直调，团队协作工具不可用降级执行）**：排障手（架构） + 设计师（前端 UX） + 质量门神（构建部署）

---

## 📌 TL;DR（执行摘要）

- 整体结论：🟢 通过（Go）
- 已完成三件事：① 梳理前后端架构与职责边界；② 前端交互/布局/视觉反馈优化并构建通过；③ 容器化部署到本地 Docker Desktop，服务可访问、配置与本地一致。
- 阻塞项：0（部署验证全绿；仅本环境 Docker Hub 对 debian-slim 镜像拉取异常，已用 alpine 规避）
- 下一步：可选——将前端 `npm run build` 接入 CI、对 antd 做分包优化、补充端到端冒烟测试。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟢 Go |
| 严重度分布 | 🔴 0 / 🟠 2（Docker 镜像拉取、CORS/KB 容器化）/ 🟡 3（布局响应式、首屏加载态、删除确认）/ 🟢 若干 |
| 关键行动项 | 5 条（见下） |
| 建议负责人 | 前端/运维 自跟进 |

---

## 1. 各成员核心结论

### 🔍 排障手（架构分析）
- 核心判断：后端「router（薄）→ service（厚）」分层清晰、职责单一、可测试性好；前端状态集中于 `App.tsx` + `useReview.ts`，结构合理。前后端职责边界清晰——重计算/外部依赖全在后端，前端纯展示+状态编排。
- 关键发现：CORS 仅放行 localhost:5173/4173；`kb_base_url` 在容器内失效；文件存内存重启即丢；`runtime_config.json` 含明文密钥；`App.tsx` 编排偏重。
- 产出：`deliverables/gstack/architecture-analysis-2026-08-11.md`

### 🎨 设计师（前端交互优化）
- 核心判断：原前端基础体验已较好（暗色日志、状态色、空态、loading 齐备），短板在响应式、删除误操作防护、连通/加载可见性。
- 关键改动：① 左/右栏响应式（大屏双栏、小屏堆叠）；② 文件删除加 Popconfirm 二次确认；③ 首屏加载全局 Spin 防误触；④ 顶栏后端连通状态灯；⑤ 审核连接期占位提示；⑥ 表格空态引导文案。
- 构建：`npm run build` 通过（✓ 3025 modules，产物 `dist/`），未引入新依赖、未改后端。
- 产出：`deliverables/gstack/designer-frontend-ux-2026-08-11.md`

### ✅ 质量门神（构建与部署）
- 核心判断：容器可正常启动、服务可访问、配置与本地一致，达到上线标准。
- 关键修复：知识库地址在容器内改为 `host.docker.internal:8000`（经 `BCR_KB_BASE_URL` + `extra_hosts`）；前端用 nginx 反代 `/api` 实现同源，规避 CORS，精确复刻本地 vite 代理。
- 障碍处置：本环境 Docker Hub 对 node/python(debian-slim) 镜像层拉取失败、仅 alpine 可拉取 → 前端改「本地 build + nginx:alpine 托管」，后端改 `python:3.13-alpine`（musllinux 轮子免编译）。
- 验证全绿：容器 healthy/Up、前端 200、反代 `/api/health` 200、容器内连通宿主机知识库 200。
- 产出：`deliverables/gstack/deploy-docker-2026-08-11.md`

---

## 2. 综合审查发现（去重合并后按严重度排序）

| # | 严重度 | 类别 | 位置 | 问题描述 | 建议 | 来源 |
|---|--------|------|------|---------|------|------|
| 1 | 🟠 | 部署 | backend/main.py + compose | CORS 仅放行 localhost:5173/4173，容器同源反代已规避 | 生产如需跨域，按环境注入 origins | 排障手/质量门神 |
| 2 | 🟠 | 部署 | config.py / runtime_config.json | `kb_base_url=localhost:8000` 容器内失效 | 已用 `BCR_KB_BASE_URL=host.docker.internal:8000` 修正 | 排障手/质量门神 |
| 3 | 🟡 | 前端 | App.tsx | 固定双栏，窄屏不堆叠 | 已改为响应式 `xs/sm/md/lg` | 设计师 |
| 4 | 🟡 | 前端 | App.tsx | 首屏异步加载期间界面空白、可误触 | 已加 `booting` 全局 Spin | 设计师 |
| 5 | 🟡 | 前端 | FilePanel.tsx | 文件删除无二次确认 | 已加 Popconfirm | 设计师 |
| 6 | 🟢 | 前端 | ProgressPanel.tsx | 审核连接期无占位 | 已加 Spin 占位 | 设计师 |
| 7 | 🟢 | 前端 | App.tsx | 无后端连通反馈 | 已加顶栏状态灯 | 设计师 |
| 8 | 🟢 | 架构 | App.tsx | 编排偏重（~310 行） | 后续可拆 Header/ReviewOptions 子组件 | 排障手 |

---

## ✅ 行动清单

| # | 行动 | 负责方 | 紧急度 | 期望完成 |
|---|------|--------|--------|---------|
| 1 | 将前端 `npm run build` 纳入 CI，避免手动预构建 dist 才能出前端镜像 | 前端/DevOps | P1 | 本迭代 |
| 2 | 生产环境用 `BCR_*` 环境变量注入密钥，避免 `runtime_config.json` 落盘明文 | 后端/运维 | P1 | 上线前 |
| 3 | `uploads/`/解析缓存按需挂 volume，或前端明确提示「重启重传」 | 后端 | P2 | 下迭代 |
| 4 | antd 做 `manualChunks` 分包或路由级懒加载，消除 >500kB 单包提示 | 前端 | P3 | 后续优化 |
| 5 | 补充端到端冒烟测试（上传→审核→导出），固化回归 | QA | P2 | 下迭代 |

---

## 📦 交付清单（全流程交付）

- **代码变更**：前端 5 文件（App.tsx / FilePanel.tsx / ProgressPanel.tsx / api.ts / index.css）；新增 Docker 全套（compose + 2 Dockerfile + nginx.conf + .dockerignore）。
- **测试覆盖**：`npm run build` 通过（类型+构建）；部署后实跑健康检查、反代链路、知识库连通验证。
- **发布检查清单**：镜像构建 ✅ / 容器启动 ✅ / 健康检查 ✅ / 前端可访问 ✅ / `/api` 反代 ✅ / KB 覆盖生效 ✅。
- **回滚预案**：`docker compose down` 移除容器即回滚；镜像保留可 `up` 恢复；数据（内存）重启重传。

---

## ⚠️ 待完善 / 已知局限

- 前端镜像依赖本地预构建 `dist`（因本环境 node 镜像不可拉取）；若改回多阶段自构建，需在能拉取 node 镜像的环境进行。
- 部署验证为基础设施级（健康检查/反代/连通）；真实「上传文件→大模型审核→导出报告」端到端流程依赖外部千问/OCR/知识库服务在线，未在此跑完整业务流。
- 文件存储于内存，容器重启需重新上传（与本地一致，属已知产品限制）。

---

## 📚 成员产出索引

- 排障手（架构）原始产出：`deliverables/gstack/architecture-analysis-2026-08-11.md`
- 设计师（前端 UX）原始产出：`deliverables/gstack/designer-frontend-ux-2026-08-11.md`
- 质量门神（部署）原始产出：`deliverables/gstack/deploy-docker-2026-08-11.md`

---

> 本报告由软件工坊 AI 协作生成（团队协作工具不可用，主理人降级为单 Agent 直调模式执行并汇编）。关键决策请由工程负责人复核。
