# 架构分析报告 · 投标文件合规审核工具

**日期**：2026-08-11
**分析范围**：`biddingfiles-Review` 整体代码结构（前端 + 后端）
**分析方法**：静态阅读 backend/ 与 frontend/src/ 全部关键文件，结合 README 与运行时配置

---

## 1. 项目定位

本工具是面向招投标场景的**文档合规性与一致性审核系统**：上传招标/投标/附件多文件，由后端调用千问 80B 大模型做规则化审核，SSE 实时回传阶段进度、知识库检索轨迹与逐条结论，最终可导出 Word 报告。本质是「单页前端 + 异步 API 网关 + 大模型编排引擎」三层结构，三个外部服务（LLM / OCR / 知识库）通过后端 service 层封装。

---

## 2. 目录与模块组织

### 2.1 后端（`backend/`）

```
backend/
├── __main__.py            # python -m backend 入口，uvicorn 0.0.0.0:8100
├── main.py                # FastAPI 应用、CORS、路由挂载、/api/health
├── config.py              # 配置中心：DEFAULTS + 环境变量(BCR_) + runtime_config.json 热更新持久化
├── runtime_config.json    # 运行时配置落盘（含密钥，需脱敏）
├── models/schemas.py      # Pydantic 数据模型（Rule/RuleSet/UploadedFile/ReviewRequest/Finding/...）
├── routers/               # HTTP 层（薄）：参数校验 + 调用 service
│   ├── files.py           # 文件上传/列表/角色标注/原文定位/文本提取/删除
│   ├── review.py          # POST /review/stream —— SSE 审核主流程
│   ├── rules.py           # 规则集列表/按模式取规则/增删
│   ├── settings.py        # 配置读取/更新/重置/连通性测试/知识库列表
│   └── export.py          # POST /export/report —— Word 报告生成
└── services/              # 业务逻辑层（厚）
    ├── review_engine.py   # 审核编排核心：文件解析→招标要求提取→规则分批送审→一致性核查→汇总
    ├── llm_client.py      # 千问客户端（OpenAI 兼容 + JSON 稳健解析 + 工具降级）
    ├── ocr_client.py      # 图聆云 OCR
    ├── kb_client.py       # 知识库 SSE 聚合检索
    ├── doc_parser.py      # PDF/Word/Excel 解析（扫描页自动 OCR）
    ├── file_store.py      # 文件存储与解析缓存（内存）
    ├── rules_store.py     # 内置规则 + 自定义规则集持久化
    ├── prompts.py         # 审核提示词模板
    ├── report_exporter.py # Word 报告渲染
    ├── text_locator.py    # 证据原文定位（精确→标准化→模糊匹配）
    └── web_search_client.py # 可选联网搜索（tavily/bing/serper）
```

**分层评价**：后端采用清晰的「router（薄）→ service（厚）」分层，职责单一、可测试性好。`review_engine` 是复杂度核心，聚合了所有外部依赖，是后续可测试性改造的重点。

### 2.2 前端（`frontend/src/`）

```
frontend/src/
├── main.tsx               # React 入口（挂载 AntdApp 以提供 message/notification 上下文）
├── App.tsx                # 主布局：Header + Row(Col9 左栏 / Col15 右栏)
├── index.css              # 全局样式（.app-header / .app-body 等）
├── types/index.ts         # 前端类型（与后端 schema 对应）
├── services/api.ts        # API 客户端：fetch 封装 + SSE 流读取（streamReview）
├── hooks/useReview.ts      # 审核流状态机：running/logs/progress/findings/issues/kbTraces/summary
└── components/
    ├── FilePanel.tsx       # 文件上传、角色标注、选择、删除
    ├── RulePanel.tsx       # 规则集选择、规则勾选
    ├── ProgressPanel.tsx   # 阶段进度、日志、知识库检索轨迹
    ├── ResultPanel.tsx     # 逐条结论、一致性问题、摘要、导出
    ├── SettingsDrawer.tsx  # 服务配置抽屉（地址、连通性测试、知识库选择）
    └── SnippetViewer.tsx   # 证据原文片段查看
```

**分层评价**：前端状态集中在 `App.tsx`（文件/规则/配置）+ `useReview.ts`（审核流），组件以 props 受控。结构清晰，但 `App.tsx` 承担了过多编排逻辑（≈310 行），可进一步拆分 `ReviewOptions` / `Header` 子组件。

---

## 3. API 面（`/api` 前缀）

| 域 | 方法 & 路径 | 作用 |
|---|---|---|
| files | POST `/files/upload` | 多文件上传（FormData: files + roles） |
| files | GET `/files` | 文件列表 |
| files | PATCH `/files/{id}/role` | 标注角色（tender/bid/attachment） |
| files | POST `/files/locate` | 证据原文定位 |
| files | GET `/files/{id}/text` | 取解析文本 |
| files | DELETE `/files/{id}` | 删除文件 |
| review | POST `/review/stream` | SSE 审核主流程 |
| rules | GET `/rulesets`、GET `/rulesets/by-mode/{mode}`、CRUD | 规则集 |
| settings | GET/PATCH `/settings`、POST `/settings/reset` | 配置读写 |
| settings | POST `/settings/test`、GET `/settings/knowledge-bases` | 连通性测试、知识库列表 |
| export | POST `/export/report` | 导出 Word |
| 系统 | GET `/api/health` | 健康检查（返回三个外部服务地址） |

---

## 4. 前后端职责划分

| 关注点 | 前端 | 后端 |
|---|---|---|
| 文件解析（PDF/Word/Excel、OCR） | ❌ | ✅ doc_parser + ocr_client |
| 审核编排 / 大模型调用 | ❌ | ✅ review_engine + llm_client |
| 知识库检索、联网搜索 | ❌ | ✅ kb_client / web_search_client |
| 规则定义与分层 | 仅展示与勾选 | ✅ rules_store + prompts |
| SSE 流消费、进度可视化 | ✅ useReview + ProgressPanel | ✅ 产生事件 |
| UI 状态、交互、报告渲染入口 | ✅ | 生成报告字节流 ✅ |
| 配置持久化 | 仅读写界面 | ✅ config + runtime_config.json |

**结论**：职责边界**清晰且合理**——重计算与外部依赖全部在后端，前端是纯展示 + 状态编排。前端通过 `/api` 代理访问后端，SSE 用原生 `fetch` + `ReadableStream` 手动解析（规避了 EventSource 仅支持 GET 的限制），实现稳健。

**唯一越界点**：`runtime_config.json` 含 `web_search_api_key` 等密钥，后端 `public_config()` 已脱敏，但 `runtime_config.json` 明文落盘，容器化需注意挂载与权限。

---

## 5. 关键设计点

1. **配置体系（config.py）**：`DEFAULTS` → `runtime_config.json` → 环境变量 `BCR_<KEY>` 三级覆盖，优先级明确；`update_config` 只接受已知键，避免注入。这是容器化的关键钩子——所有外部地址都可经 `BCR_` 在 compose 中覆盖。
2. **SSE 审核流**：后端 `review.py` 用 `StreamingResponse` 推送 `ReviewEvent` JSON；前端 `streamReview` 手动按 `\n\n` 切分、按 `data:` 解析，支持 `AbortController` 取消。
3. **规则分层（BID 17 / TENDER 12 / GENERAL 8）**：按 `mode` 自动装配规则集，支持规则级过滤与自定义规则集持久化（`rules_store.json`）。
4. **工具调用降级**：vLLM 未开 `--enable-auto-tool-choice` 时，自动从 function calling 降级为「提示词规划」，功能等价、多一次调用。

---

## 6. 架构 / 部署问题清单

| # | 严重度 | 问题 | 影响 |
|---|---|---|---|
| 1 | 🔴 | **CORS 仅放行 `localhost:5173/4173`**（`main.py`） | 容器化后前端由 nginx 提供（非 localhost 源），预检会被拒；需改为按环境注入 origins 或用同源反代 |
| 2 | 🔴 | **`kb_base_url` 默认 `http://localhost:8000`** | 在后端容器内 `localhost` 指向容器自身，知识库在宿主机，必须改为 `host.docker.internal:8000`；LLM/OCR 用外部 IP 不受影响 |
| 3 | 🟠 | **文件存储于内存**（`file_store.py`） | 容器重启即丢，需挂载 volume 或改为后端重启后前端重新上传（当前设计即此） |
| 4 | 🟠 | **`runtime_config.json` 明文含密钥** | 容器内落盘需限制权限，或改由 `BCR_` 环境变量注入、不写盘 |
| 5 | 🟡 | **`App.tsx` 编排过重（~310 行）** | 维护成本，建议拆 `Header` / `ReviewOptions` 子组件 |
| 6 | 🟡 | **无健康检查探针 / 就绪检查** | 容器编排缺 `HEALTHCHECK`，依赖 SSE 长连接需注意超时 |
| 7 | 🟡 | **`__main__.py` 绑定 `127.0.0.1`** | 容器内若以此入口启动则外部不可达；compose 应直接用 `uvicorn` 命令绑定 `0.0.0.0` |

---

## 7. 架构层面改进建议

1. **Docker 化配置策略**：前端用 nginx 反代 `/api` → `backend:8100` 实现同源，彻底规避 CORS（#1）；后端入口统一 `uvicorn 0.0.0.0:8100`；KB 地址经 `BCR_KB_BASE_URL=http://host.docker.internal:8000` 覆盖（#2/#7）。
2. **密钥与配置分离**：生产环境用 `BCR_` 环境变量注入密钥，将 `runtime_config.json` 设为可选、不落盘密钥（#4）。
3. **状态持久化**：将 `uploads/` 与解析缓存挂 volume，或接受「重启重传」并在前端明确提示（#3）。
4. **前端拆分**：将 `App.tsx` 中的 Header、审核选项卡片抽为独立组件，降低单文件复杂度（#5）。
5. **可观测性**：为后端加 `HEALTHCHECK` 与 `/api/health` 探活；SSE 加心跳保活，避免反代超时断流（#6）。

---

> 本报告由软件工坊主理人直调执行（团队协作工具不可用，降级为单 Agent 直调模式）。关键决策请由工程负责人复核。
