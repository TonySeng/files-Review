"""文档合规审核工具 - 后端服务入口。"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from . import config
from .routers import (
    files,
    review,
    rules,
    settings,
    export,
    feedback,
    file_types,
    rulegroups,
    legalrules,
    prompts,
    reviewdata,
    audit,
    call_audit,
    auth,
    admin,
)
from .services import call_audit as _call_audit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动：启动孤儿任务周期回收器（兜底清理进程内 worker 被取消/崩溃遗留的僵尸任务）
    from .services import task_store

    try:
        task_store.get_store().start_reaper()
    except Exception as exc:  # noqa: BLE001
        logging.warning("启动孤儿任务回收器失败: %s", exc)

    # 存储配置热加载器：监视 data/storage_config.json，修改后自动切换存储后端
    import asyncio

    from .storage import hot as storage_hot

    storage_reload_task = asyncio.create_task(storage_hot.run_hot_reload())
    yield

    storage_reload_task.cancel()


API_DESCRIPTION = """
对招标文件、投标文档及相关附件进行**合规性审核**与**跨文件一致性核查**，支持法规文件 LLM 规则挖掘、
审核报告导出、反馈沉淀与历史数据管理。

## 鉴权方式

| 调用方 | 凭据 | 传递方式 |
|---|---|---|
| 管理员 | 会话令牌（`POST /api/auth/login` 获取） | 请求头 `X-Session-Token: <token>`，有效期 7 天 |
| 集成调用 | API Key（用户中心自助生成） | 请求头 `X-API-Key: <key>`，按 `scopes` 授权 |

- 管理员可见全部数据；普通用户/API Key 仅能访问自身归属的数据。
- API Key 缺少对应 scope 时返回 `403`；未认证返回 `401`。
- 密钥类配置在读取接口中脱敏回显（`***`），更新时传 `***` 或空值表示不修改。

## 通用约定

- 所有接口前缀 `/api`；健康检查 `GET /api/health` 无需鉴权。
- 审核支持两种执行方式：`POST /api/review/stream`（SSE 实时推送）与 `POST /api/review/tasks`（后台异步任务 + 轮询）。
- 文件先经 `POST /api/files/upload` 上传取得 `file_id`，再在审核/规则生成接口中引用。
- 分页接口统一使用 `page`/`page_size` 查询参数。
"""

app = FastAPI(
    title="文档合规审核工具",
    description=API_DESCRIPTION,
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# 本地工具，仅开放给本机前端开发端口
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(files.router)
app.include_router(review.router)
app.include_router(rules.router)
app.include_router(settings.router)
app.include_router(export.router)
app.include_router(feedback.router)
app.include_router(file_types.router)
app.include_router(rulegroups.router)
app.include_router(legalrules.router)
app.include_router(prompts.router)
app.include_router(reviewdata.router)
app.include_router(audit.router)
app.include_router(call_audit.router)
app.include_router(auth.router)
app.include_router(auth.admin)
app.include_router(auth.user_keys)
app.include_router(admin.router)


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """记录每一次业务接口调用：方法、路径、状态码、耗时、客户端、是否 5xx，以及调用方身份。

    写盘通过 asyncio.to_thread 提交，避免阻塞事件循环；审计失败不影响主流程。
    /api/health 等高频探活接口跳过，减少噪声。
    调用方身份（user_id / role / api_key_prefix）由鉴权依赖注入 request.state，
    在本 finally 阶段（端点执行完成后）读取并落盘。
    """
    path = request.url.path
    if path in ("/api/health",):
        return await call_next(request)
    req_id = uuid.uuid4().hex[:12]
    start = time.monotonic()
    client = request.client.host if request.client else ""
    status = 500
    has_error = False
    resp_size = None
    try:
        response = await call_next(request)
        status = response.status_code
        has_error = status >= 500
        cs = response.headers.get("content-length")
        resp_size = int(cs) if cs and cs.isdigit() else None
        return response
    except Exception:
        has_error = True
        raise
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        user_id = getattr(request.state, "user_id", None)
        role = getattr(request.state, "user_role", None)
        api_key_prefix = getattr(request.state, "api_key_prefix", None)
        try:
            # fire-and-forget：审计写盘不阻塞响应，且绝不影响主流程。
            # 目标部署服务器（约 20 容器共存）线程受限，原 await asyncio.to_thread
            # 在默认线程池枯竭时会抛 RuntimeError 并冒泡到 finally，可能把正常请求打挂；
            # 改为 run_in_executor 非阻塞提交 + 外层 try/except 吞掉一切异常。
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _call_audit.log_api_call(
                    ts=time.time(),
                    req_id=req_id,
                    method=request.method,
                    path=path,
                    status=status,
                    duration_ms=duration_ms,
                    client=client,
                    has_error=has_error,
                    resp_size=resp_size,
                    user_id=user_id,
                    role=role,
                    api_key_prefix=api_key_prefix,
                ),
            )
        except Exception:  # noqa: BLE001
            # 审计记录失败（含线程受限）绝不影响主流程
            pass


@app.get("/api/health")
async def health():
    cfg = config.get_config()
    return {
        "status": "ok",
        "services": {
            "llm": cfg["llm_base_url"],
            "ocr": cfg["ocr_base_url"],
            "kb": cfg["kb_base_url"],
        },
    }
