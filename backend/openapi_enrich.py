"""OpenAPI 文档增强层。

零侵入地为自动生成的 OpenAPI（/api/openapi.json，对应 /api/docs 页面）注入：
1. 「枚举值速查表」——全系统使用的枚举/常量取值与中文释义；
2. 各接口「贴近真实业务」的请求/响应示例。

本模块不修改任何路由业务逻辑，仅在 `app.openapi()` 生成 schema 后做后处理。
所有示例取值均为演示数据（如「中标价与暂估价异常接近」规则、172.7 万元等），
仅用于说明字段含义，不指向任何真实项目。
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 1) 枚举值速查表（追加到 API description 顶部章节）
# ---------------------------------------------------------------------------
ENUM_REFERENCE = """
## 枚举值 / 常量取值速查表

> 本系统所有接口的取值受以下枚举约束。未在此列出的字段一般为自由文本或具体业务 ID。

### 审核模式 `mode`（审核对象类型）
| 取值 | 含义 |
|---|---|
| `bid` | 投标文件审核（默认） |
| `tender` | 招标文件审核 |
| `general` | 通用文档审核（不触发招标/投标特定策略与一致性核查） |

### 文件角色 `role`（上传文件在审核中的定位）
| 取值 | 含义 |
|---|---|
| `tender` | 招标文件 |
| `bid` | 投标文件（默认） |
| `attachment` | 附件（图纸、资质证明等） |
| `legal` | 法规文件（用于 LLM 规则挖掘，非审核目标） |

### 审核结论 `status`（单条规则判定）
| 取值 | 含义 |
|---|---|
| `pass` | 合规 / 通过 |
| `fail` | 不合规 / 未通过 |
| `warn` | 警告（提示性风险，非硬性不合规） |
| `unknown` | 待人工复核（模型判定不确定或自相矛盾时降级） |

### 规则类别 `category`
| 取值 | 含义 |
|---|---|
| `qualification` | 资格性审查 |
| `commercial` | 商务标审查 |
| `technical` | 技术标审查 |
| `format` | 格式符合性 |
| `consistency` | 跨文件一致性 |
| `tender_quality` | 招标文件质量 |
| `general_quality` | 通用文档质量 |
| `legal` | 法规符合性 |

### 严重级别 `severity`
| 取值 | 含义 |
|---|---|
| `critical` | 严重（一票否决级） |
| `major` | 主要（默认） |
| `minor` | 次要 |
| `info` | 提示 |

### 任务状态 `task.status`（审核任务生命周期）
| 取值 | 含义 | 终态 |
|---|---|---|
| `pending` | 排队中 | 否 |
| `running` | 审核进行中 | 否 |
| `completed` | 已完成 | 是 |
| `failed` | 失败 | 是 |
| `cancelled` | 已取消 | 是 |

### 法规规则集状态 `legal-rules.status`
| 取值 | 含义 | 终态 |
|---|---|---|
| `pending` | 待处理 | 否 |
| `mining` | LLM 抽取中 | 否 |
| `ready` | 已就绪（可引用/固化） | 是 |
| `failed` | 抽取失败 | 是 |

### 反馈判定 `judgment`（校验集 / 结论反馈）
| 取值 | 含义 |
|---|---|
| `adopt` | 采纳模型结论 |
| `reject` | 驳回 / 修正 |

### 文件校验等级 `FileValidation.level`
| 取值 | 含义 |
|---|---|
| `ok` | 通过 |
| `warn` | 警告（可继续审核） |
| `error` | 错误（解析失败，不可审核） |

### 用户角色 `role`（账号维度）
| 取值 | 含义 |
|---|---|
| `admin` | 管理员（会话令牌 `X-Session-Token`，可看全部数据） |
| `user` | 普通用户（`X-API-Key` 调用，仅见自身归属数据） |

### API Key 授权范围 `scopes`（网关按路径前缀校验，缺失返回 403）
| 取值 | 覆盖路径前缀 |
|---|---|
| `review` | `/api/review`、`/api/files`、`/api/reviewdata` |
| `feedback` | `/api/feedback` |
| `kb` | `/api/knowledge`、`/api/settings/knowledge` |
| `audit` | `/api/audit` |
| `rules` | `/api/rulesets`、`/api/rule-groups`、`/api/legal-rules`、`/api/file-types` |
| `settings` | `/api/settings` |
| `*` | 通配，拥有全部范围 |

### 外部服务供应商（配置项，非接口入参枚举）
| 配置项 | 取值 | 含义 |
|---|---|---|
| `ocr_provider` | `tuling` / `baidu` / `xfyun` | 图聆云 / 百度智能云 / 讯飞开放平台 OCR |
| `llm_base_url` + `llm_model` | 自定义 | LLM 兼容接口地址与模型名（如 SiliconFlow、本地千问） |

### 一致性核查结论（跨文件要素比对）
`inconsistency_rate`（0~1，越高越不一致）、`rule_drifted`（bool，规则内容是否相对存证基线漂移）、
`deterministic_ratio`（结构化引擎锁定的确定性结论占比，越高结果越稳定）。
"""

# ---------------------------------------------------------------------------
# 2) 接口示例：用 builder 构造，避免手工嵌套括号出错
# ---------------------------------------------------------------------------
# 示例数据格式（贴近真实业务）：
#   request  : { ctype: { 示例名: (summary, value), ... }, ... }
#   responses: { code  : { ctype: { 示例名: (summary, value), ... }, ... }, ... }


def _body(content: dict[str, dict[str, tuple[str, Any]]]) -> dict[str, Any]:
    return {
        "content": {
            ctype: {
                "examples": {
                    name: {"summary": summary, "value": value}
                    for name, (summary, value) in examples.items()
                }
            }
            for ctype, examples in content.items()
        }
    }


EXAMPLES: dict[str, dict[str, dict[str, Any]]] = {}


def reg(path: str, method: str, request: dict | None = None,
        responses: dict | None = None) -> None:
    op: dict[str, Any] = {}
    if request:
        op["requestBody"] = _body(request)
    if responses:
        op["responses"] = {code: _body(body) for code, body in responses.items()}
    EXAMPLES.setdefault(path, {})[method.lower()] = op


# ---------------- 鉴权 auth ----------------
reg("/api/auth/login", "post",
    request={"application/json": {"登录获取会话令牌": ("管理员/用户登录",
              {"username": "admin", "password": "admin123"})}},
    responses={"200": {"application/json": {"登录成功": ("返回会话令牌",
              {"username": "admin", "role": "admin",
               "token": "sess_9f3c2a7b1e4d5f6a", "token_ttl": 604800})}}})

reg("/api/auth/register", "post",
    request={"application/json": {"自助注册待审核账号": ("注册",
              {"username": "reviewer01", "password": "Passw0rd!",
               "email": "reviewer01@example.com", "company": "鑫智链"})}},
    responses={"200": {"application/json": {"注册成功": ("返回用户信息并自动下发 API Key",
              {"id": "u-1001", "username": "reviewer01", "role": "user",
               "status": "pending_review", "api_key": "ak_live_3c8f9a2b1d"})}}})

reg("/api/auth/me", "get",
    responses={"200": {"application/json": {"当前用户信息": ("当前登录用户",
              {"id": "u-1001", "username": "reviewer01", "role": "user",
               "scopes": ["review", "feedback"], "created_at": "2026-09-01T10:20:30Z"})}}})

# ---------------- 文件 files ----------------
reg("/api/files/upload", "post",
    request={"multipart/form-data": {"多文件上传（form 字段说明）": ("上传招标文件/投标文件",
              {"files": "(二进制文件，可多个) 招标文件.docx、投标文件.pdf",
               "roles": "tender,bid   # 与 files 顺序一致，逗号分隔：tender/bid/attachment/legal",
               "file_types": "ft-b6329f6d,   # 可选，用户手动指定的文件类型 id，逗号分隔，缺省留空"})}},
    responses={"200": {"application/json": {"上传结果": ("返回解析后的文件元信息",
              {"files": [
                  {"file_id": "f-20260908-001", "filename": "招标文件.docx", "size": 248320,
                   "ext": "docx", "role": "tender", "file_type": "ft-b6329f6d",
                   "char_count": 18230, "page_count": 12, "used_ocr": False,
                   "parse_error": None,
                   "validation": {"ok": True, "level": "ok", "messages": []}}],
               "errors": []})}}})

reg("/api/files", "get",
    responses={"200": {"application/json": {"已上传文件列表": ("当前会话文件",
              {"files": [
                  {"file_id": "f-20260908-001", "filename": "招标文件.docx", "size": 248320,
                   "ext": "docx", "role": "tender", "file_type": "ft-b6329f6d",
                   "char_count": 18230, "page_count": 12, "used_ocr": False,
                   "parse_error": None, "validation": {"ok": True, "level": "ok", "messages": []}}]})}}})

reg("/api/files/{file_id}/role", "patch",
    request={"application/json": {"修改文件角色": ("把文件重新归类为投标附件",
              {"role": "attachment"})}},
    responses={"200": {"application/json": {"更新后文件": ("返回更新后的文件元信息",
              {"file_id": "f-20260908-001", "filename": "补充说明.pdf",
               "role": "attachment", "file_type": None})}}})

reg("/api/files/{file_id}/type", "patch",
    request={"application/json": {"修改文件类型（触发重新解析）": ("改绑文件类型",
              {"file_type": "ft-b6329f6d"})}},
    responses={"200": {"application/json": {"更新后文件": ("返回更新后的文件元信息",
              {"file_id": "f-20260908-001", "filename": "招标文件.docx",
               "role": "tender", "file_type": "ft-b6329f6d"})}}})

# ---------------- 审核 review ----------------
reg("/api/review/tasks", "post",
    request={"application/json": {"创建投标审核任务": ("引用已上传文件与规则集启动异步审核",
              {"file_ids": ["f-20260908-001", "f-20260908-002"], "mode": "bid",
               "ruleset_id": "custom-mthy9akr", "rule_ids": None,
               "file_types": ["ft-b6329f6d", None], "rule_group_ids": ["rg-bid-main"],
               "auto_match": True, "kb_enabled": True, "kb_id": "kb-legal-001",
               "web_search_enabled": False,
               "extra_instruction": "重点关注中标价与暂估价接近度，以及资质门槛一致性",
               "deterministic_mode": True})}},
    responses={"200": {"application/json": {"任务已创建": ("返回 task_id，轮询详情获取结论",
              {"task_id": "t-20260908-a1b2c3", "status": "pending"})}}})

reg("/api/review/tasks/{task_id}", "get",
    responses={"200": {"application/json": {"任务详情与逐条结论": ("已完成任务的完整 findings",
              {"task_id": "t-20260908-a1b2c3", "status": "completed", "mode": "bid",
               "progress": 100, "progress_message": "审核完成",
               "file_ids": ["f-20260908-001", "f-20260908-002"],
               "rule_count": 117, "created_at": "2026-09-08T09:30:11Z",
               "finished_at": "2026-09-08T09:33:55Z",
               "findings": [
                   {"rule_id": "custom-mthy9akr",
                    "rule_name": "中标价与暂估价异常接近（疑似泄露暂估价）",
                    "category": "commercial", "severity": "major", "status": "pass",
                    "title": "中标价与暂估价差异充分，判定合规",
                    "detail": "A=暂估价172.7万元，B=中标价152.00万元，差值207000元，"
                              "差异率11.99%（阈值10.00%，以A为分母），R≥10% → pass",
                    "evidence": "第1页「暂估价(万元) 172.7」；第2页「中标价格152.000000万元」",
                    "location": "评标报告.docx 第1页/第2页", "suggestion": "",
                    "legal_basis": "", "involved_files": ["评标报告.docx"], "confidence": 1.0}]})}}})

reg("/api/review/tasks", "get",
    responses={"200": {"application/json": {"任务列表": ("分页任务列表",
              {"tasks": [
                  {"task_id": "t-20260908-a1b2c3", "status": "completed", "mode": "bid",
                   "progress": 100, "created_at": "2026-09-08T09:30:11Z"}],
               "total": 1, "page": 1, "page_size": 20})}}})

reg("/api/review/stream", "post",
    request={"application/json": {"SSE 实时审核请求": ("与 /tasks 同参，返回 SSE 事件流",
              {"file_ids": ["f-20260908-001", "f-20260908-002"], "mode": "bid",
               "ruleset_id": "custom-mthy9akr", "deterministic_mode": True})}},
    responses={"200": {"text/event-stream": {"SSE 事件流": ("事件类型示例",
              "event: progress\ndata: {\"task_id\":\"t-...\",\"status\":\"running\",\"progress\":42}\n\n"
              "event: finding\ndata: {\"rule_id\":\"custom-mthy9akr\",\"status\":\"pass\"}\n\n"
              "event: done\ndata: {\"task_id\":\"t-...\",\"status\":\"completed\"}\n\n")}}})

# ---------------- 规则集 rulesets ----------------
reg("/api/rulesets", "post",
    request={"application/json": {"新建自定义规则集（含确定性与 LLM 规则）": ("保存含 amount_pair_diff 相对阈值的规则",
              {"name": "投标商务标专项规则集",
               "description": "聚焦报价与暂估价接近度等商务风险",
               "rules": [
                   {"id": "r-price-close",
                    "name": "中标价与暂估价异常接近（疑似泄露暂估价）",
                    "category": "commercial", "severity": "major", "enabled": True,
                    "doc_types": ["ft-b6329f6d"],
                    "description": "中标价与暂估价完全一致或差异<暂估价10%则判定不合规",
                    "checkpoints": [
                        "定位项目暂估价 A 与最终中标候选人投标价 B（缺一直接判 unknown，禁止用概算价替代）",
                        "计算差异率 R=|A-B|/A，与 10% 比较",
                        "R=0 或 R<10% → fail；R≥10% → pass"],
                    "structured": {
                        "amount_pair_diff": [
                            {"a_field": ["暂估价"], "b_field": ["中标价", "投标价", "预中标价"],
                             "max_ratio": 0.1, "ratio_base": "a", "fail_when": "lt"}]}}]})}},
    responses={"200": {"application/json": {"保存结果": ("返回规则集 id",
              {"id": "rs-custom-20260908", "name": "投标商务标专项规则集",
               "rules": 1, "builtin": False})}}})

reg("/api/rulesets/by-mode/{mode}", "get",
    responses={"200": {"application/json": {"按模式取规则": ("bid 模式规则预览",
              {"mode": "bid", "rules": [
                  {"id": "r-price-close", "name": "中标价与暂估价异常接近（疑似泄露暂估价）",
                   "category": "commercial", "severity": "major", "enabled": True}]})}}})

reg("/api/rulesets/consistency-elements", "get",
    responses={"200": {"application/json": {"一致性核查要素清单": ("跨文件一致性比对要素",
              {"elements": [
                  {"key": "bidder_name", "label": "投标人名称", "enabled": True},
                  {"key": "bid_price_total", "label": "投标总价", "enabled": True},
                  {"key": "tender_no", "label": "招标编号", "enabled": True}]})}}})

reg("/api/rulesets/consistency-preview", "post",
    request={"application/json": {"预览一致性入口配置": ("传入待核查要素",
              {"elements": ["bidder_name", "bid_price_total"]})}},
    responses={"200": {"application/json": {"预览结果": ("返回命中的要素入口",
              {"preview": [
                  {"key": "bidder_name", "found_in": ["招标文件.docx", "投标文件.pdf"],
                   "consistent": True}]})}}})

# ---------------- 法规规则集 legal-rules ----------------
reg("/api/legal-rules/generate", "post",
    request={"application/json": {"由法规文件生成临时规则集": ("引用 role=legal 文件做 LLM 抽取",
              {"file_ids": ["f-20260908-009"], "name": "政府采购法专项",
               "description": "从政府采购法提取投标合规性规则", "mode": "bid",
               "max_rules": 30, "is_shared": False, "reuse": True})}},
    responses={"200": {"application/json": {"生成任务已提交": ("返回规则集 id（状态 mining）",
              {"ruleset_id": "lr-20260908-7a", "status": "mining",
               "message": "法规规则集生成中，稍后查询详情"})}}})

reg("/api/legal-rules", "get",
    responses={"200": {"application/json": {"法规规则集列表": ("概要含 rule_count",
              {"rulesets": [
                  {"id": "lr-20260908-7a", "name": "政府采购法专项", "status": "ready",
                   "rule_count": 24, "is_shared": False, "created_at": "2026-09-08T08:10:00Z"}]})}}})

reg("/api/legal-rules/{ruleset_id}", "get",
    responses={"200": {"application/json": {"法规规则集详情": ("含规则明细与过程日志",
              {"id": "lr-20260908-7a", "name": "政府采购法专项", "status": "ready",
               "rule_count": 24, "rules": [
                   {"id": "lr-rule-01", "name": "投标有效期不得短于招标文件要求",
                    "category": "qualification", "severity": "major"}],
               "logs": [{"step": "chunk", "detail": "切分 12 块"}]})}}})

reg("/api/legal-rules/{ruleset_id}/promote", "post",
    request={"application/json": {"固化为正式规则集": ("可选改名",
              {"name": "政府采购法正式规则集"})}},
    responses={"200": {"application/json": {"固化结果": ("生成正式 ruleset",
              {"id": "rs-promoted-001", "source": "lr-20260908-7a", "ok": True})}}})

# ---------------- 章节库 sections ----------------
reg("/api/sections", "post",
    request={"application/json": {"新建/更新章节": ("按文件类型维度裁剪规则作用域",
              {"id": "sec-001", "file_type_id": "ft-b6329f6d",
               "name": "投标人须知前附表", "synonyms": ["投标须知前附表", "前附表"],
               "note": "资质门槛与投标有效期集中处", "enabled": True})}},
    responses={"200": {"application/json": {"章节已保存": ("返回章节记录",
              {"id": "sec-001", "file_type_id": "ft-b6329f6d",
               "name": "投标人须知前附表", "builtin": False, "enabled": True})}}})

# ---------------- 文件类型 file-types ----------------
reg("/api/file-types", "post",
    request={"application/json": {"新建/更新文件类型": ("关联规则组",
              {"id": "ft-b6329f6d", "name": "施工招标投标文件",
               "description": "房建施工类招标/投标文件", "extensions": ["docx", "pdf"],
               "rule_group_ids": ["rg-bid-main"]})}},
    responses={"200": {"application/json": {"保存结果": ("返回文件类型",
              {"id": "ft-b6329f6d", "name": "施工招标投标文件",
               "extensions": ["docx", "pdf"], "builtin": False})}}})

# ---------------- 规则组 rule-groups ----------------
reg("/api/rule-groups", "post",
    request={"application/json": {"新建/更新规则组": ("规则组聚合若干规则",
              {"id": "rg-bid-main", "name": "投标主规则组",
               "description": "商务+技术+资格主流程",
               "rule_ids": ["r-price-close", "r-qual-001"]})}},
    responses={"200": {"application/json": {"保存结果": ("返回规则组",
              {"id": "rg-bid-main", "name": "投标主规则组",
               "rule_ids": ["r-price-close", "r-qual-001"], "builtin": False})}}})

# ---------------- 反馈 feedback ----------------
reg("/api/feedback", "post",
    request={"application/json": {"提交结论反馈": ("采纳或驳回某条 finding",
              {"task_id": "t-20260908-a1b2c3", "rule_id": "r-price-close",
               "finding": {"status": "pass", "title": "差异充分判合规"},
               "judgment": "adopt", "reject_reason": None})}},
    responses={"200": {"application/json": {"提交成功": ("返回反馈记录",
              {"id": "fb-20260908-01", "judgment": "adopt", "ok": True})}}})

reg("/api/feedback", "get",
    responses={"200": {"application/json": {"反馈记录列表": ("分页反馈",
              {"feedback": [
                  {"id": "fb-20260908-01", "rule_id": "r-price-close", "judgment": "adopt",
                   "created_at": "2026-09-08T10:05:00Z"}], "total": 1})}}})

reg("/api/feedback/validation", "get",
    responses={"200": {"application/json": {"校验集（沉淀样本）": ("已沉淀判定样本",
              {"items": [
                  {"id": "vi-001", "rule_id": "r-price-close", "status": "accepted",
                   "finding": {"status": "pass", "title": "差异充分判合规"}}], "total": 1})}}})

# ---------------- Prompt 模板 prompts ----------------
reg("/api/prompts/{key}", "put",
    request={"application/json": {"更新 Prompt 模板": ("自动生成新版本",
              {"content": "你是一名资深招标合规审核专家……（完整提示词）",
               "comment": "强化结论与文本一致性约束"})}},
    responses={"200": {"application/json": {"更新结果": ("返回新版本号",
              {"key": "review_rule", "version": 7, "updated_at": "2026-09-08T10:00:00Z"})}}})

reg("/api/prompts/{key}/test", "post",
    request={"application/json": {"测试 Prompt 渲染": ("可选真实调用 LLM",
              {"variables": {"rule_count": "117"}, "send_to_llm": False})}},
    responses={"200": {"application/json": {"渲染结果": ("返回渲染后文本",
              {"key": "review_rule", "rendered": "（注入 117 条规则后的完整提示词）",
               "llm_response": None})}}})

reg("/api/prompts/{key}/reset", "post",
    responses={"200": {"application/json": {"重置结果": ("回退到内置默认版本",
              {"key": "review_rule", "version": 8, "reset_to": "builtin"})}}})

# ---------------- 系统配置 settings ----------------
reg("/api/settings", "get",
    responses={"200": {"application/json": {"配置（密钥脱敏）": ("密钥以 *** 回显",
              {"llm_base_url": "http://223.111.149.152:8000", "llm_model": "/model",
               "llm_api_key": "***", "ocr_provider": "tuling",
               "ocr_api_key": "***", "kb_enabled": True, "concurrency": 10,
               "llm_max_input_tokens": 60000, "findings_cache_enabled": True})}}})

reg("/api/settings", "patch",
    request={"application/json": {"更新配置（白名单字段）": ("传 *** 或空表示不修改该密钥",
              {"ocr_provider": "xfyun", "llm_max_input_tokens": 30000, "kb_enabled": True})}},
    responses={"200": {"application/json": {"更新后配置": ("返回最新配置",
              {"ocr_provider": "xfyun", "llm_max_input_tokens": 30000,
               "kb_enabled": True, "llm_api_key": "***"})}}})

reg("/api/settings/test", "post",
    request={"application/json": {"连通性测试": ("测试 LLM/OCR/KB",
              {"target": "llm"})}},
    responses={"200": {"application/json": {"测试结果": ("返回各服务状态",
              {"llm": {"ok": True, "latency_ms": 820},
               "ocr": {"ok": True, "latency_ms": 450},
               "kb": {"ok": False, "error": "知识库服务未启动"}})}}})

reg("/api/settings/knowledge-bases", "get",
    responses={"200": {"application/json": {"可用知识库": ("KB 列表",
              {"knowledge_bases": [
                  {"id": "kb-legal-001", "name": "法律法规库", "type": "milvus"}]})}}})

# ---------------- 历史审核数据 reviewdata ----------------
reg("/api/reviewdata/records", "get",
    responses={"200": {"application/json": {"历史记录列表": ("分页历史审核记录",
              {"records": [
                  {"record_id": "rec-20260908-01", "task_id": "t-20260908-a1b2c3",
                   "mode": "bid", "file_count": 2, "rule_count": 117,
                   "created_at": "2026-09-08T09:33:55Z"}], "total": 1, "page": 1, "page_size": 20})}}})

reg("/api/reviewdata/records/{record_id}", "get",
    responses={"200": {"application/json": {"历史记录详情": ("含完整结论与文件指纹",
              {"record_id": "rec-20260908-01", "task_id": "t-20260908-a1b2c3",
               "mode": "bid", "file_md5s": ["a1b2c3..."], "rule_count": 117,
               "decisions": [{"id": "dec-001", "rule_id": "r-price-close", "judgment": "adopt"}]})}}})

reg("/api/reviewdata/records/{record_id}/decision", "patch",
    request={"application/json": {"更新人工决策": ("DecisionUpdate 结构",
              {"decision_id": "dec-001", "judgment": "adopt", "reject_reason": None})}},
    responses={"200": {"application/json": {"更新结果": ("返回决策记录",
              {"decision": {"id": "dec-001", "judgment": "adopt"}, "ok": True})}}})

reg("/api/reviewdata/verify", "post",
    request={"application/json": {"完整性校验": ("防篡改校验",
              {"record_id": "rec-20260908-01"})}},
    responses={"200": {"application/json": {"校验结果": ("返回哈希比对",
              {"record_id": "rec-20260908-01", "integrity_ok": True,
               "stored_hash": "a1b2c3...", "computed_hash": "a1b2c3..."})}}})

reg("/api/reviewdata/lookup", "post",
    request={"application/json": {"按要素查询": ("按投标人名称等检索历史数据",
              {"key": "bidder_name", "value": "南京大六文化传媒有限公司"})}},
    responses={"200": {"application/json": {"查询结果": ("命中历史记录",
              {"matches": [{"record_id": "rec-20260908-01", "task_id": "t-20260908-a1b2c3"}]})}}})

# ---------------- 一致性监控 audit ----------------
reg("/api/audit/consistency", "get",
    responses={"200": {"application/json": {"一致性核查汇总": ("规则漂移与确定性占比",
              {"sampled": 5, "inconsistency_rate": 0.0, "rule_drifted": False,
               "replay_ready": True, "deterministic_ratio": 0.94,
               "items": [
                   {"task_id": "t-20260908-a1b2c3", "rule_drifted": False,
                    "deterministic_ratio": 0.95}]})}}})

reg("/api/audit/records", "get",
    responses={"200": {"application/json": {"审核记录（含过滤）": ("按 task_id 查",
              {"records": [
                  {"task_id": "t-20260908-a1b2c3", "mode": "bid", "rule_count": 117,
                   "status": "completed", "created_at": "2026-09-08T09:33:55Z"}], "total": 1})}}})

# ---------------- 调用审计 call_audit ----------------
reg("/api/audit/calls", "get",
    responses={"200": {"application/json": {"外部调用审计": ("API Key 维度外部调用",
              {"calls": [
                  {"id": "c-001", "caller_key": "ak_live_3c8f9a2b1d", "endpoint": "/api/review/tasks",
                   "status": 200, "latency_ms": 210, "called_at": "2026-09-08T09:30:11Z"}],
               "total": 1})}}})

reg("/api/audit/ai-calls", "get",
    responses={"200": {"application/json": {"大模型调用审计": ("模型/耗时/token",
              {"calls": [
                  {"id": "a-001", "model": "/model", "prompt_tokens": 5400,
                   "completion_tokens": 320, "latency_ms": 820,
                   "called_at": "2026-09-08T09:31:02Z"}], "total": 1})}}})

reg("/api/audit/call-stats", "get",
    responses={"200": {"application/json": {"调用统计汇总": ("按天/接口聚合",
              {"by_day": [{"date": "2026-09-08", "count": 18, "avg_latency_ms": 640}],
               "by_endpoint": [{"endpoint": "/api/review/tasks", "count": 12}]})}}})

# ---------------- 导出 export ----------------
reg("/api/export/report", "post",
    request={"application/json": {"导出审核报告": ("docx 或 pdf",
              {"task_id": "t-20260908-a1b2c3", "format": "docx"})}},
    responses={"200": {"application/octet-stream": {"文件流": ("返回二进制文件流",
              "（docx/pdf 文件二进制流，响应头 Content-Disposition: attachment; "
              "filename=审核报告_t-20260908-a1b2c3.docx）")}}})

# ---------------- 管理员 admin ----------------
reg("/api/admin/tasks", "get",
    responses={"200": {"application/json": {"全部任务（管理员）": ("跨用户任务",
              {"tasks": [
                  {"task_id": "t-20260908-a1b2c3", "user_id": "u-1001", "status": "completed",
                   "mode": "bid"}], "total": 1})}}})

reg("/api/admin/feedback", "get",
    responses={"200": {"application/json": {"全部反馈（管理员）": ("跨用户反馈",
              {"feedback": [
                  {"id": "fb-20260908-01", "user_id": "u-1001", "rule_id": "r-price-close",
                   "judgment": "adopt"}], "total": 1})}}})


# ---------------------------------------------------------------------------
# 注入逻辑
# ---------------------------------------------------------------------------
def _apply_body(target: dict, spec: dict[str, Any] | None) -> None:
    if not spec:
        return
    body = target.setdefault("requestBody", {"content": {}})
    content = body.setdefault("content", {})
    content.update(spec.get("content", {}))


def enrich_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    """对生成的 OpenAPI schema 做后处理：追加枚举表 + 注入接口示例。"""
    info = schema.setdefault("info", {})
    info["description"] = (info.get("description") or "") + "\n\n" + ENUM_REFERENCE

    paths = schema.get("paths", {})
    for path, methods in EXAMPLES.items():
        item = paths.get(path)
        if not item:
            continue
        for method, spec in methods.items():
            op = item.get(method)
            if not op:
                continue
            if "requestBody" in spec:
                _apply_body(op, spec["requestBody"])
            if "responses" in spec:
                responses = op.setdefault("responses", {})
                for code, body in spec["responses"].items():
                    responses.setdefault(code, {})
                    _apply_body(responses[code], body)
    return schema
