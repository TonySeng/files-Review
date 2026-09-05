"""API 数据模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["critical", "major", "minor", "info"]
RuleCategory = Literal[
    "qualification", "commercial", "technical", "format", "consistency",
    "tender_quality", "general_quality", "legal"
]
ReviewMode = Literal["bid", "tender", "general"]


class Rule(BaseModel):
    id: str
    name: str
    category: RuleCategory = "qualification"
    description: str = ""
    # 审核要点，逐条喂给模型
    checkpoints: list[str] = Field(default_factory=list)
    severity: Severity = "major"
    enabled: bool = True
    # 是否倾向于检索法规知识库
    需要法规依据: bool = Field(default=False, alias="need_legal_basis")
    builtin: bool = False
    # 关联文档类型：仅对这些文件类型的文件执行本规则审核；空列表=适用于全部文件
    doc_types: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class RuleSet(BaseModel):
    id: str
    name: str
    description: str = ""
    rules: list[Rule] = Field(default_factory=list)
    builtin: bool = False


class UploadedFile(BaseModel):
    file_id: str
    filename: str
    size: int
    ext: str
    # role: tender/bid/attachment 为审核目标；legal 为「法规依据文件」——
    # 仅用于自动生成临时审核规则，不得作为审核目标送审（review._resolve_docs 会拦截）。
    role: Literal["tender", "bid", "attachment", "legal"] = "bid"
    file_type: str | None = None  # 用户手动指定的文件类型 id（系统不自动识别）
    char_count: int = 0
    page_count: int = 0
    used_ocr: bool = False
    parse_error: str | None = None
    # 上传/解析阶段的校验结论（格式/大小/内容规则），前端据此展示状态与异常
    validation: "FileValidation | None" = None


class FileValidation(BaseModel):
    """单文件校验结论：格式、大小、内容规则。level 决定前端状态色。"""

    ok: bool = True
    level: Literal["ok", "warn", "error"] = "ok"
    messages: list[str] = Field(default_factory=list)


class FileType(BaseModel):
    """可审核的文件类型，关联规则组用于自动匹配。"""

    id: str
    name: str
    description: str = ""
    extensions: list[str] = Field(default_factory=list)  # 允许后缀；空=不限制
    rule_group_ids: list[str] = Field(default_factory=list)  # 关联规则组
    builtin: bool = False


class RuleGroup(BaseModel):
    """规则组：规则的命名组合，可多选套用到任务。"""

    id: str
    name: str
    description: str = ""
    rule_ids: list[str] = Field(default_factory=list)
    builtin: bool = False


class ReviewRequest(BaseModel):
    file_ids: list[str]
    mode: ReviewMode = "bid"  # 审核模式（向后兼容）
    ruleset_id: str | None = None
    rule_ids: list[str] | None = None
    # 法规临时规则集（2026-09-03 新增）：由上传的法律法规文件自动生成，
    # 与上述既有来源并存——非空时其规则并入本次送审规则（按 id 去重）。
    legal_ruleset_ids: list[str] | None = None
    # 是否「只用法规临时规则」审核：
    #   True  = 完全不使用内置规则/规则组/规则集，只跑法规自动生成的规则；
    #   False = 强制与既有来源叠加；
    #   None（默认）= 自动判定：未显式选择任何既有来源（规则组/真实规则集/规则清单）
    #                 时视为「只用法规规则」，否则叠加。
    legal_rules_only: bool | None = None
    # 通用化审核：文件类型（与 file_ids 顺序一致，可缺省）与规则组多选
    # 允许元素为 null/空（前端对未指定类型的文件传 null），后端仅作记录、不参与路由
    file_types: list[str | None] | None = None
    rule_group_ids: list[str] | None = None  # 审核规则组多选
    auto_match: bool = False  # 是否依手动指定的文件类型自动匹配规则组
    kb_enabled: bool | None = None
    kb_id: str | None = None
    web_search_enabled: bool | None = None  # 是否启用联网搜索
    extra_instruction: str = ""
    # 确定性执行：重跑时可显式携带历史版本指纹，强制「按历史版本重跑」以保证可复现。
    # 不传则由引擎按当前生效版本自动解析（确定性模式默认开启）。
    deterministic_mode: bool | None = None
    # 重放请求：携带历史任务 id，引擎沿用其完整版本清单（引擎/规则/数据快照/解析器/
    # 配置参数）重跑，用于验证「相同输入+相同版本 → 相同输出」。
    replay_from_task_id: str | None = None

    @field_validator("file_types")
    @classmethod
    def _norm_file_types(cls, v):
        # 前端对未指定类型的文件传 null（如 [null, "ft-bid"]）；过滤掉 null/空元素，
        # 避免 Pydantic 因 null 元素报 "Input should be a valid string"。
        # 该字段后端不参与路由（类型由 docs 自身决定），仅作任务记录。
        if v is None:
            return None
        return [t for t in v if t]


class LegalRuleGenerateRequest(BaseModel):
    """由法规文件生成临时审核规则集。同步返回记录，规则在后台异步抽取。"""

    file_ids: list[str]  # 法规文件（role=legal/attachment 均可，不要求是审核目标）
    name: str = ""
    description: str = ""
    mode: ReviewMode = "bid"  # 后续审核对象：bid 投标文件 / tender 招标文件
    max_rules: int | None = None  # 规则数上限，缺省取配置 legal_max_rules
    is_shared: bool = False
    reuse: bool = True  # 同源同参已生成过则复用，避免重复消耗


class LegalRuleUpdateRequest(BaseModel):
    """编辑临时规则集：改名/改说明/整体替换规则/改共享范围。"""

    name: str | None = None
    description: str | None = None
    is_shared: bool | None = None
    rules: list[dict[str, Any]] | None = None


class LegalRulePromoteRequest(BaseModel):
    """把临时规则集固化为正式自定义规则集。"""

    name: str | None = None


class PromptUpdateRequest(BaseModel):
    """编辑提示词模板：保存为当前内容的新版本（可带变更备注）。"""

    content: str
    comment: str = ""


class PromptRollbackRequest(BaseModel):
    """回滚提示词模板到指定历史版本（生成一条复制内容的新版本）。"""

    version: int


class PromptTestRequest(BaseModel):
    """提示词模板测试验证：变量渲染 + 可选真实调用大模型查看效果。"""

    variables: dict[str, str] = {}
    send_to_llm: bool = False


class Finding(BaseModel):
    rule_id: str
    rule_name: str = ""
    category: str = ""
    severity: Severity = "major"
    status: Literal["pass", "fail", "warn", "unknown"] = "unknown"
    title: str = ""
    detail: str = ""
    evidence: str = ""
    location: str = ""
    suggestion: str = ""
    legal_basis: str = ""
    involved_files: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    # 错别字类识别的结构化信息（用于校验集去噪与跨审核去重）；非错别字 finding 为 null
    typo: "TypoInfo | None" = None


class TypoInfo(BaseModel):
    wrong: str  # 原文错字/词
    correct: str  # 应改正字/词
    context: str = ""  # 含错字的原句


class FeedbackSubmit(BaseModel):
    task_id: str | None = None
    rule_id: str
    finding: dict[str, Any] = Field(default_factory=dict)
    judgment: Literal["adopt", "reject"]
    reject_reason: str | None = None


class FeedbackQuery(BaseModel):
    judgment: Literal["adopt", "reject"] | None = None
    rule_id: str | None = None
    is_typo: bool | None = None
    q: str | None = None
    limit: int = 50
    offset: int = 0


class ValidationItem(BaseModel):
    id: str
    finding_fingerprint: str
    task_id: str | None = None
    rule_id: str
    typo_wrong: str | None = None
    typo_correct: str | None = None
    original_finding: dict[str, Any] | None = None
    status: str
    created_at: str
    updated_at: str


class ValidationFilter(BaseModel):
    id: str
    typo_wrong: str
    typo_correct: str | None = None
    reject_reason: str | None = None
    feedback_id: str
    active: bool
    hit_count: int
    created_at: str
    updated_at: str
    last_hit_at: str | None = None
    deactivate_reason: str | None = None  # 停用补充说明（停用过滤规则时必填）


class ValidationStatusUpdate(BaseModel):
    """错别字校验集列表内直接更新判定状态（采纳/驳回/待判定）。"""

    status: str  # accepted | rejected | pending
    reject_reason: str | None = None  # 驳回时必填补充说明


class FilterDeactivateRequest(BaseModel):
    """停用过滤规则时必填的停用补充说明。"""

    deactivate_reason: str


class FeedbackStats(BaseModel):
    total: int
    adopt: int
    reject: int
    typo_total: int
    active_filters: int
    validation_items: int


class ConsistencyIssue(BaseModel):
    field: str
    severity: Severity = "major"
    description: str = ""
    values: list[dict[str, str]] = Field(default_factory=list)
    suggestion: str = ""


class ConfigUpdate(BaseModel):
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_timeout: int | None = None
    llm_temperature: float | None = None
    llm_max_tokens: int | None = None
    ocr_provider: str | None = None
    ocr_base_url: str | None = None
    ocr_path: str | None = None
    ocr_category: str | None = None
    ocr_api_key: str | None = None
    ocr_secret_key: str | None = None
    ocr_token_path: str | None = None
    ocr_xfyun_app_id: str | None = None
    ocr_xfyun_api_key: str | None = None
    ocr_xfyun_api_secret: str | None = None
    ocr_timeout: int | None = None
    kb_base_url: str | None = None
    kb_id: str | None = None
    kb_api_key: str | None = None
    kb_timeout: int | None = None
    kb_enabled: bool | None = None
    kb_max_queries: int | None = None
    web_search_enabled: bool | None = None
    web_search_api: str | None = None
    web_search_api_key: str | None = None
    web_search_max_results: int | None = None
    web_search_timeout: int | None = None
    max_chars_per_doc: int | None = None
    concurrency: int | None = None
    llm_max_concurrent: int | None = None  # 全局并发闸：同时打到 LLM 提供方的在途请求上限
    rules_per_batch: int | None = None  # 每批送审规则数（方案C：6 → 10）
    findings_cache_enabled: bool | None = None  # 方案C：相同输入批次复用历史结论、跳过 LLM 调用的全局开关
    consistency_cache_enabled: bool | None = None  # 一致性切片摘要缓存开关（提效①）
    consistency_max_concurrent: int | None = None  # 一致性阶段并发上限（提效②，默认串行）
    consistency_enabled: str | None = None  # 一致性核查开关：auto(规则驱动) / on(强制) / off(关闭)
    # 确定性执行相关配置
    deterministic_mode: bool | None = None
    deterministic_temperature: float | None = None
    data_snapshot_version: str | None = None
    use_current_time_as_basis: bool | None = None
    # 校对类规则（错别字 / 语义 / 术语）分段并行校对（2026-09-02）：
    # 这类规则需逐字扫描全文，大文档下单次调用必然超过 llm_timeout 并撞
    # llm_call_hard_ceil（表现为「批次审核失败: llm call exceeded hard ceiling 540s」）。
    # 文档超过 editing_seg_chars 即切段并行审核。详见 config.py 同名配置项注释。
    editing_seg_chars: int | None = None  # 校对类规则单段字符长度
    editing_max_segments: int | None = None  # 校对类规则最大段数
    # 法规文件 → 临时审核规则（2026-09-03 新增）
    legal_chunk_chars: int | None = None  # 法规分块抽取的单块字符数
    legal_max_rules: int | None = None  # 单个临时规则集保留规则数上限
    legal_mining_concurrency: int | None = None  # 分块抽取并发上限
    legal_max_source_chars: int | None = None  # 法规源文本处理上限
    legal_mining_timeout: int | None = None  # 单块抽取读超时（秒）
    legal_mining_stream: bool | None = None  # 法规挖掘是否走流式调用
    llm_stream_idle_timeout: int | None = None  # 流式调用相邻增量之间的空窗超时（秒）
    legal_staleness_years: int | None = None  # 法规版本距今超过 N 年提示“可能已被修订/废止”


class ConnectionTestRequest(BaseModel):
    target: Literal["llm", "ocr", "kb", "web_search"]
    base_url: str | None = None
    api_key: str | None = None
    secret_key: str | None = None  # 需要 AK/SK 双密钥的服务（如百度 OCR / 讯飞 OCR 的 APISecret）
    app_id: str | None = None  # 讯飞 OCR 等需要 AppID 的服务，先测后存时临时生效
    provider: str | None = None  # OCR 服务类型（tuling / baidu / xfyun），先测后存时临时生效


# --------------------------------------------------------------------------- #
# 权限管理：用户 / 登录 / 会话
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str  # 管理员会话令牌（X-Session-Token）
    user: "UserOut"


class UserCreate(BaseModel):
    username: str
    display_name: str | None = None
    role: str = "user"  # admin | user
    password: str | None = None  # 仅管理员需要


class UserUpdate(BaseModel):
    display_name: str | None = None
    is_active: bool | None = None
    password: str | None = None  # 修改密码（管理员或自身）


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str | None = None
    role: str
    status: str = "active"  # pending | active | disabled | rejected
    api_key_prefix: str | None = None
    api_key: str | None = None  # 不再回显完整密钥（安全加固）
    is_active: bool = True
    created_at: str | None = None
    created_by: str | None = None
    last_login_at: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    api_keys: list[dict[str, Any]] = Field(default_factory=list)  # 脱敏后的密钥列表


class ApiKeyRotateResponse(BaseModel):
    user_id: str
    api_key: str  # 完整 key，仅此次返回
    api_key_prefix: str


# --------------------------------------------------------------------------- #
# 注册与 API Key 管理
# --------------------------------------------------------------------------- #
class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None


class ApiKeyCreate(BaseModel):
    name: str | None = None
    scopes: list[str] | None = None  # 如 ["review","kb"]；空或 ["*"] 表示全部


class ApiKeyUpdate(BaseModel):
    name: str | None = None
    status: str | None = None  # active | disabled | revoked
    scopes: list[str] | None = None


class ApiKeyOut(BaseModel):
    key_id: str
    prefix: str
    name: str | None = None
    status: str = "active"
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    created_at: str | None = None
    last_used_at: str | None = None
    created_by: str | None = None


class ApiKeyCreatedResponse(BaseModel):
    key_id: str
    api_key: str  # 完整 key，仅此次返回
    prefix: str
    name: str | None = None
    status: str = "active"
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    created_at: str | None = None
