# 部署到 131 服务器 —— 更新清单（final13）

生成时间：2026-09-07　基准包：final12（2026-09-05 18:33）
部署包：`bidding-review-arm64-final13.tar.gz`（675 KB / 691,518 字节）

---

## 一、本次更新包含的功能

| # | 功能 | 说明 |
|---|---|---|
| 1 | **规则-章节关联** | 章节库以文件类型为维度维护（预置 33 条），规则可多选「关联章节」，审核时只审命中章节正文，其余跳过 |
| 2 | **章节库维度下拉化** | 顶部维度选择由横向平铺改为下拉单选，默认「全部」，避免维度增加后横向溢出 |
| 3 | **上下文超长自动降级** | 发前 token 预算 + 反应式三级降级（丢 KB → 截正文 50% → 20%），根治上线后频繁的模型上下文超长 400 |

---

## 二、更新的文件清单（20 项）

### 后端（9 项）

| 文件 | 类型 | 改动要点 |
|---|---|---|
| `backend/services/sections.py` | **新增** | 章节库核心：按文件类型的持久化（collection `sections`）、33 条预置章节、标题识别与正文切分、归一化匹配（精确>前缀>包含，同级最长优先）、同义词冲突检测 |
| `backend/routers/sections.py` | **新增** | 接口：`GET /api/sections`、`POST /api/sections`（返回 warnings）、`DELETE /api/sections/{id}`、`GET /api/sections/parse`、`POST /api/sections/preview` |
| `backend/main.py` | 修改 | 注册 `sections` 路由 |
| `backend/config.py` | 修改 | 新增 `llm_max_input_tokens`(60000)、`llm_context_overflow_max_degrade`(3) |
| `backend/models/schemas.py` | 修改 | `Rule` 新增 `section_ids`；两项新配置登记进 `ConfigUpdate` 白名单（否则 PATCH 会被静默丢弃） |
| `backend/services/rules_store.py` | 修改 | `_normalize_rule` 字段白名单加入 `section_ids`（不加则保存被静默丢弃） |
| `backend/services/versioning.py` | 修改 | `rule_content_hash` 纳入 `doc_types`/`section_ids`，改章节后缓存自动失效 |
| `backend/services/review_engine.py` | 修改 | ①章节裁剪 `_batch_section_ids`/`_section_scoped`；②上下文超长降级 `_run_rule_prompt_guarded` + 等比截断 `_truncate_docs_text`；③`_run_with_kb` 超长立即上抛（防同 prompt 重发）；④`_run_rule_per_file` 全失败上抛（防异常被吞） |
| `backend/services/llm_client.py` | 修改 | 新增 `LLMContextOverflow`（继承 `LLMError`，不破坏既有兜底）、`_is_context_overflow`（覆盖 OpenAI/vLLM/中文报错文案）、`estimate_tokens`（CJK 1.6、其余 0.3，保守多估）；`chat`/`chat_stream` 的 400 分支命中即抛 |

### 前端（9 项）

| 文件 | 类型 | 改动要点 |
|---|---|---|
| `frontend/src/pages/SectionManagementPage.tsx` | **新增** | 章节库配置页：文件类型下拉切换维度（默认「全部」）、表格 CRUD、同义词编辑 |
| `frontend/src/components/SynonymTagsInput.tsx` | **新增** | 通用同义词输入组件（与一致性要素同款交互） |
| `frontend/src/types/index.ts` | 修改 | 新增 `Section` 类型；`Rule` 增加 `section_ids` |
| `frontend/src/services/api.ts` | 修改 | 章节接口封装 |
| `frontend/src/App.tsx` | 修改 | 菜单新增「章节库配置」入口 + 页面路由 |
| `frontend/src/components/RuleEditorModal.tsx` | 修改 | 新增「关联章节」多选（候选随已选文档类型联动，留空=审全文） |
| `frontend/src/components/RuleDetailDrawer.tsx` | 修改 | 规则详情展示关联章节 |
| `frontend/src/pages/RuleManagementPage.tsx` | 修改 | 规则卡片展示章节 Tag、`sectionMap` 映射 |
| `frontend/src/components/ConsistencyElementsEditor.tsx` | 修改 | 同义词输入改为复用 `SynonymTagsInput`，两处交互统一 |

### 构建产物与脚本（2 项）

| 文件 | 改动 |
|---|---|
| `frontend/dist/` | 重新构建，产物 `index-BjBmj2sU.js`（含「章节库配置」「本任务强制禁用缓存」等新文案，已 grep 校验） |
| `make-deploy-package.sh` | `FINAL=final13`；前端构建增加 `npm run build` 失败回退直调 vite |
| `transfer-to-131.sh` | 默认包名改为 `bidding-review-arm64-final13.tar.gz` |

---

## 三、部署步骤

### 方式 A：一键传输（推荐，需先填 IP）

```bash
# 本机执行（SERVER_IP 必填）
SERVER_IP=<131服务器IP> SERVER_USER=root ./transfer-to-131.sh
```

脚本自动完成：scp 上传 → ssh 解压到 `/opt/` → 执行 `deploy.sh`（`docker-compose up -d --build`）。

### 方式 B：手动

```bash
scp bidding-review-arm64-final13.tar.gz <user>@<131服务器IP>:/opt/
ssh <user>@<131服务器IP>
cd /opt && tar xzf bidding-review-arm64-final13.tar.gz
cd bidding-review-deploy && ./deploy.sh
```

部署后访问：前端 `http://<IP>:8180`、后端探活 `http://<IP>:8100/api/health`。

---

## 四、部署后必须确认的 5 件事

1. **模型上下文窗口**（最重要）：默认 `llm_max_input_tokens=60000` 是按 128k 窗口留余量设的。131 若跑本地千问 80B 等小窗口模型，需调低该值，否则仍会超长。现在可在线改，不必改码：
   ```
   PATCH /api/settings  {"llm_max_input_tokens": 30000}
   ```
   关闭自动降级（不建议）用 `llm_context_overflow_max_degrade: 0`。
2. **章节库自动预置**：无需导入，首次打开「章节库配置」会自动 seed 33 条预置章节（collection 为空时触发）。
3. **配置不同步**：`app.db` 不随部署包同步，131 上已有的 LLM / OCR 凭据、规则集、用户数据保持原样，不受影响。
4. **前端强刷**：浏览器 `Ctrl+F5`，避免旧 `index.html` 缓存。
5. **131 专用配置已保留**：包内 `docker-compose.yml` 含 `security_opt: seccomp=unconfined`（Docker 18.09 无 clone3 白名单的必需修复），`Dockerfile` 与 `requirements.txt` 仍是 arm64 专用版本（python:3.13-slim / 纯 uvicorn / PyMuPDF==1.26.0 / faulthandler），未被本地版覆盖。

---

## 五、部署后验证

```bash
# 1) 服务健康
curl http://<IP>:8100/api/health          # 期望 200
curl -o /dev/null -w "%{http_code}" http://<IP>:8180/   # 期望 200

# 2) 章节接口（需先登录取 token）
curl "http://<IP>:8100/api/sections" -H "X-Session-Token: <token>"

# 3) 界面
#    功能菜单 → 配置管理 → 章节库配置：下拉选「全部」应看到 33 条跨类型章节
#    规则管理 → 编辑规则：可见「关联章节」多选
#    跑一次真实审核任务：观察是否仍有「上下文超长」失败；若降级会 emit 提示
```

---

## 六、回滚

`bidding-review-arm64-final12.tar.gz` 保留在项目根目录，回滚只需把它按同样步骤传到 131 解压并 `./deploy.sh`（数据卷不受影响）。
