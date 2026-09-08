"""运行期配置管理：支持环境变量初始化 + 运行时热更新 + 持久化到 SQLite 数据库。

持久化优先级（高→低）：环境变量(BCR_*) > SQLite 数据库 > 本地 JSON(兼容/迁移) > 默认值。
数据库作为主持久化层，可跨容器重建保留（需将数据库目录挂载到 Docker 卷）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

CONFIG_FILE = Path(__file__).parent / "runtime_config.json"

# 数据目录：默认位于 backend/data（容器化时挂载为持久卷 backend_data，跨容器重建保留）
DATA_DIR = Path(os.getenv("BCR_DATA_DIR", str(Path(__file__).parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 数据库文件：默认位于 backend/data/app.db，可用 BCR_DB_PATH 覆盖（容器化时挂载卷）
DB_PATH = Path(
    os.getenv("BCR_DB_PATH", str(DATA_DIR / "app.db"))
)

# 默认值取自实际部署环境（已验证连通）
DEFAULTS: dict[str, Any] = {
    # 千问 80B（vLLM，OpenAI 兼容协议）
    "llm_base_url": "http://223.111.149.152:8000",
    "llm_model": "/model",
    "llm_api_key": "EMPTY",
    "llm_timeout": 180,
    "llm_temperature": 0.1,
    "llm_max_tokens": 8192,
    "llm_max_retries": 3,  # 单次对话遇空响应/网络抖动的重试次数（一致性/批次共用）；5→3 根除早期"5×180≈900s 单调用挂死"
    "rule_retry_on_missing": 1,  # 模型漏答/整批失败时，对缺失规则做针对性补答重试次数
    "batch_timeout": 900,  # 单规则批（含预检索 KB + 主 LLM + 缺失补答）的总墙钟硬上限（秒）。KB 开启后单批含多路慢调用，600s 常被突破 → 提到 900s 留余量；单 LLM 调用另有 llm_call_hard_ceil 独立封顶
    # 审核并发与批处理
    "concurrency": 10,  # 并发审核的规则批次数（默认 3 → 6 → 10，2026-08-31 提升：SiliconFlow+本地 KB 均验证可承载更高并发）
    "llm_max_concurrent": 10,  # 全局并发闸：同时打到 LLM 提供方的在途请求上限（2026-08-31 随 concurrency 提到 10，与批次并发匹配）
    "rules_per_batch": 1,  # 按规则逐条送审：每条规则一次独立 LLM 调用，并行受 concurrency 控制；失败隔离、单请求体量更小
    "rule_per_file_enabled": True,  # 多文件非编辑类规则：按"文件"切片并行（同源分段思路），消除 N×60k 巨型 prompt 撞 540s 硬顶
    "rule_per_file_concurrent": 6,  # 单批次内"按文件并行"的并发上限（受全局 llm_max_concurrent 二次收口）
    "findings_cache_enabled": True,  # 方案C：相同输入批次复用历史结论，跳过 LLM 调用
    "kb_prefetch_global": True,  # 按规则按需预检索知识库：仅 need_legal_basis 的规则检索，依据只注入该规则 prompt，避免全量 KB 灌入每个请求
    # OCR（支持三种服务：tuling=图聆云 免鉴权 multipart 上传；baidu=百度智能云 OCR，AK/SK 换 access_token；
    #       xfyun=讯飞开放平台 通用文字识别 intsig，hmac-sha256 URL 签名鉴权）
    "ocr_provider": "tuling",  # tuling | baidu | xfyun
    "ocr_base_url": "http://223.111.149.152:8090",
    "ocr_path": "/tuling/uocr/v2/recognize",
    "ocr_category": "atlas.doc",  # 仅图聆云使用
    "ocr_api_key": "",  # 百度智能云 API Key（tuling 不需要）
    "ocr_secret_key": "",  # 百度智能云 Secret Key（tuling 不需要）
    "ocr_token_path": "/oauth/2.0/token",  # 百度换取 access_token 的路径（挂在 base_url 下）
    "ocr_xfyun_app_id": "",  # 讯飞开放平台 AppID（仅 xfyun 使用）
    "ocr_xfyun_api_key": "",  # 讯飞开放平台 APIKey（仅 xfyun 使用）
    "ocr_xfyun_api_secret": "",  # 讯飞开放平台 APISecret（仅 xfyun 使用）
    "ocr_timeout": 60,
    # 本地知识库（LLM-Docqa，SSE 流式问答）
    "kb_base_url": "http://host.docker.internal:8011",
    "kb_id": "",
    "kb_api_key": "",  # 可选；部分知识库部署需要鉴权令牌
    "kb_timeout": 150,  # 知识库单查询读超时（秒）。KB(LLM-Docqa) 单查需 50~80s，45s 会让预检索第一次尝试必然超时、白等一轮重试 → 提到 150 留足余量
    "kb_enabled": True,
    "kb_max_queries": 3,
    # 规则审核阶段：是否允许模型在 _run_with_kb 中「实时调用 KB 检索工具」做多轮检索。
    # 默认 False（强烈建议）：规则批仅依赖「预检索」一次性注入的法规依据（标准 RAG 模式），
    # 不让模型实时调 KB 工具。原因：KB 服务单次查询需 50~80s，模型若多轮调工具（最多 kb_max_queries 轮）
    # 会让单批累计耗时突破 batch_timeout，导致整个规则批被超时跳过（曾出现 18 规则 16 批超时）。
    # 预检索已覆盖 need_legal_basis 规则的法规依据，关闭实时工具不影响合规审核结论。
    # 仅在确需模型自主扩检、且 KB 服务足够快时再置 True。
    "kb_tool_calls_enabled": False,
    # 联网搜索（可选，用于获取最新政策、行业动态）
    "web_search_enabled": False,
    "web_search_api": "tavily",  # tavily | bing | serper
    "web_search_api_key": "",
    "web_search_max_results": 5,
    "web_search_timeout": 30,
    # 审核行为
    "max_chars_per_doc": 60000,
    "doc_summary_max_segments": 40,  # 超大文档分片摘要的最大段落数（防止极端大文档摘要成本失控）
    # 一致性核查：现已统一采用「分段摘要 → 要素提取 → 一致性校验」逻辑，
    # 单文件/多文件均逐文档分段摘要后比对，不再依赖该阈值门控（保留供未来微调）。
    # 分段长度由 consistency_seg_chars 控制。
    # 一致性核查开关（2026-09-01 起改为规则驱动）：
    #   auto（默认）= 任务规则中存在「一致性」类规则才执行，核查要点与核心要素全部取自规则配置，
    #                 于是同一套一致性配置可随规则集 / 规则组复用；规则集里不含一致性规则则不执行。
    #   on           = 强制执行（等价于旧行为：mode != "general"），要素仍按规则声明 + 要素库推断。
    #   off          = 完全关闭一致性核查阶段。
    "consistency_enabled": "auto",
    "consistency_max_chars": 40000,
    "consistency_seg_chars": 8000,  # 单文档内分段长度（一致性摘要按此切片）
    # 校对类规则（gen-typo 错别字 / gen-semantics 语义 / gen-terminology 术语）分段并行校对
    # （2026-09-02 新增）：这类规则要求模型"逐字逐句"扫描全文，单次调用要把数万字符原文
    # 一次性 prefill 后再长输出，必然超过 llm_timeout(180s)，三次重试耗尽即撞
    # llm_call_hard_ceil(540s) —— 表现为「批次审核失败: llm call exceeded hard ceiling 540s」。
    # 属结构性必然（文档够大必现），而非提供方偶发抖动。
    # 文档超过该阈值即切分为多段、每段独立调用（单段 ≈8k 字符，响应降到秒级~数十秒），
    # 并行执行后累加合并各段结论；逐段校对还让注意力更集中，命中率不降反升。
    "editing_seg_chars": 8000,  # 校对类规则单段长度（字符）
    "editing_max_segments": 40,  # 校对类规则最大段数（防止极端大文档段数失控）
    # ---- 法规文件 → 临时审核规则（Legal Ruleset Mining，2026-09-03 新增）----
    # 用户上传法律法规/规范性文件，由模型解析全文并自动抽取「可核查」的合规规则，
    # 生成一组临时规则集，直接用于后续审核（不落正式规则库，除非用户显式「转正」）。
    # 法规全文常达数十万字，一次性送 LLM 必然撞 llm_timeout 与 llm_call_hard_ceil，
    # 故按「条/章」切分为多块、并发抽取后再全局合并去重（思路同校对类分段并行）。
    "legal_chunk_chars": 3000,  # 单块送审字符数（按条边界打包，不切断条款）。注意：解码耗时主要随「每块规则条数」而非块长变化，缩小块主要价值是降低单调用超时风险
    "legal_max_rules": 30,  # 单个临时规则集保留的规则数上限（按可核查性打分截断）
    "legal_mining_concurrency": 6,  # 分块抽取的并发上限（受全局 llm_max_concurrent 二次收口）。墙钟提速主要靠并发波次而非小块化（实测 6 并发 4 块一波 387s vs 3 并发 2 波 512s）
    "legal_max_source_chars": 600000,  # 法规源文本处理上限，超出截断并告警（防极端大文件烧 token）
    "legal_mining_timeout": 420,  # 单块抽取读超时（秒）。流式模式下仅作硬顶预算基数（×3=1260s），不再约束单次尝试时长
    "legal_mining_stream": True,  # 法规挖掘走流式调用：读超时只约束增量空窗，根治「长解码>读超时被误杀」；false 回退非流式
    "llm_stream_idle_timeout": 120,  # 流式调用相邻 SSE 增量之间的空窗超时（秒）
    "legal_staleness_years": 8,  # 法规版本距今超过 N 年即提示“可能已被修订/废止”
    # 一致性核查专属读超时（秒）：一致性 material 由多文档摘要拼接，体量较大，
    # 模型需生成整份 issues JSON，单次生成耗时显著高于规则批次，故用比全局 llm_timeout
    # 更大的超时，避免 180s 读超时导致整个审核任务失败。
    "consistency_timeout": 600,
    # 一致性提效（2026-08-26）：
    # ① 切片摘要缓存：相同「文档文本哈希+模式+要素+温度」复用历史摘要，跳过重复 LLM 调用。
    "consistency_cache_enabled": True,
    # ② 一致性阶段并发上限：实测 SiliconFlow+DeepSeek-V4-Flash 在 1-6 并发下均稳定（无 stall），
    # 故取 2 兼顾大文档一致性阶段的提速与余量；若遇外部限流再下调。
    "consistency_max_concurrent": 2,
    # 一致性提效（2026-08-30）：改为「每文件并行提取要素 + 轻量结构化比对」，
    # 单文档内分段摘要改为并发（避免超大文档串行摘要拖垮单规则批次超时），
    # 一致性阶段改为每文件一次性提取结构化要素（≤ max_chars_per_doc 单调用），
    # 再对紧凑要素做跨文件比对（prompt 体量小、不触发 40k 截断/超时）。
    "doc_summary_concurrent": 4,  # 单文档内分段摘要的并发上限
    "consistency_extract_concurrent": 6,  # 一致性阶段「每文件并行提取要素」的并发上限
    # Fix A：一致性要素提取喂给模型的"紧凑表征"安全上限（字符）。源自 _build_docs_text 的分片摘要，
    # 此处再截断一次，避免极端大摘要仍撑爆上下文导致 DeepSeek-V4-Flash 分钟级慢调用。
    "consistency_element_max_chars": 20000,
    # Fix B：单逻辑 LLM 调用总墙钟硬上限（秒）。实际生效值 = min(req_timeout*3, 本值)，
    # 防止"连接挂起 + 多次重试"把一次调用拖到十几分钟拖垮整任务。
    "llm_call_hard_ceil": 1800,  # 单逻辑调用墙钟绝对上界；实际生效 = min(req_timeout×max_attempts, 本值)。提到 1800 以覆盖一致性比对(600×3)的完整重试预算，避免"1 次尝试后即被硬上限剥夺重试"
    # 上下文超长降级（Fix：上线后频繁出现大模型上下文超长 400）。
    # llm_max_input_tokens：发前 token 预算上限（保守值，为主体 128k 窗口留足输出/系统/安全余量）。
    # 拼好的 messages 估算 token 超此值即主动降级（丢弃 KB 依据→截断送审正文），不直接发必败请求；
    # 若真实返回 400 上下文超长，由 llm_client.LLMContextOverflow 触发同款多级降级。
    # 该值是「输入预算」，不含模型输出与系统提示；设为 60000 远低于窗口上限以规避边界抖动。
    "llm_max_input_tokens": 60000,
    # 上下文超长时的最大多级降级次数（0=关闭降级，直接以 LLMContextOverflow 失败；默认 3：丢 KB→截 50%→截 20%）
    "llm_context_overflow_max_degrade": 3,
    # ---- 确定性执行（可重复审核结果一致）----
    # 开关：开启后审核引擎固定规则执行顺序、temperature=0、关闭并发竞态（串行执行），
    # 不把当前系统时间作为任何判断依据，并对 LLM 输出做结构化约束与确定性归一化。
    # 关闭时回落到原有高吞吐并发模式（适合首次大批量跑、对波动不敏感的场景）。
    "deterministic_mode": False,  # 大批量默认走高吞吐非确定模式（方案A）；存证/归档可显式开启确定性重跑
    # 确定性模式下强制的采样温度（0 = 关闭采样，输出尽可能确定）
    "deterministic_temperature": 0.0,
    # 依赖数据快照版本：规则依赖的外部数据（KB 知识库等）的版本标识。
    # 审核时明确使用哪个版本，否则同一文件在不同时间可能因外部数据变化产生不同结果。
    "data_snapshot_version": "KB_BASELINE_v2026.08.01",
    # 是否把「当前系统时间」作为判定依据（强烈建议 False）。
    # 审核结论必须仅依赖文档内容与规则，不依赖运行时刻。
    "use_current_time_as_basis": False,
}

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


@contextmanager
def _db_conn():
    """取得 SQLite 连接，确保表存在。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def _load_db() -> dict[str, Any]:
    """从数据库读取已持久化的配置；异常时回退空字典。"""
    try:
        with _db_conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {k: json.loads(v) for k, v in rows}
    except Exception:
        return {}


def _save_db(patch: dict[str, Any]) -> None:
    """将补丁写入数据库（幂等 upsert）。"""
    if not patch:
        return
    try:
        with _db_conn() as conn:
            conn.executemany(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, json.dumps(v, ensure_ascii=False)) for k, v in patch.items()],
            )
    except OSError:
        pass


def _clear_db() -> None:
    try:
        with _db_conn() as conn:
            conn.execute("DELETE FROM settings")
    except Exception:
        pass


def _load() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    # 1) 本地 JSON（历史/迁移兼容）
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    # 2) 数据库（主持久化层，优先级高于本地 JSON）
    cfg.update(_load_db())
    # 3) 环境变量优先级最高（便于容器化部署覆盖）
    for key in DEFAULTS:
        env = os.getenv(f"BCR_{key.upper()}")
        if env is None:
            continue
        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                cfg[key] = env.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                cfg[key] = int(env)
            elif isinstance(default, float):
                cfg[key] = float(env)
            else:
                cfg[key] = env
        except ValueError:
            pass
    return cfg


def get_config() -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load()
        return dict(_cache)


def get(key: str, default: Any = None) -> Any:
    return get_config().get(key, default)


def update_config(patch: dict[str, Any], persist: bool = True) -> dict[str, Any]:
    """只接受已知键，避免写入任意字段。persist=False 时仅改内存（用于连通性测试）。"""
    global _cache
    with _lock:
        cfg = dict(_cache) if _cache is not None else _load()
        for key, value in patch.items():
            if key in DEFAULTS and value is not None:
                cfg[key] = value
        _cache = cfg
        if persist:
            # 主持久化：SQLite 数据库
            _save_db({k: v for k, v in patch.items() if k in DEFAULTS and v is not None})
            # 兼容镜像：同步镜像到本地 JSON（非容器场景可读写备份）
            try:
                CONFIG_FILE.write_text(
                    json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
        return dict(cfg)


def reset_config() -> dict[str, Any]:
    global _cache
    with _lock:
        _cache = dict(DEFAULTS)
        CONFIG_FILE.unlink(missing_ok=True)
        _clear_db()
        return dict(_cache)


def public_config() -> dict[str, Any]:
    """对外返回时脱敏密钥。"""
    cfg = get_config()
    if cfg.get("llm_api_key"):
        cfg["llm_api_key"] = "***"
    if cfg.get("web_search_api_key"):
        cfg["web_search_api_key"] = "***"
    if cfg.get("kb_api_key"):
        cfg["kb_api_key"] = "***"
    if cfg.get("ocr_api_key"):
        cfg["ocr_api_key"] = "***"
    if cfg.get("ocr_secret_key"):
        cfg["ocr_secret_key"] = "***"
    if cfg.get("ocr_xfyun_api_key"):
        cfg["ocr_xfyun_api_key"] = "***"
    if cfg.get("ocr_xfyun_api_secret"):
        cfg["ocr_xfyun_api_secret"] = "***"
    return cfg
