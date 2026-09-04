# 后端镜像：Python 3.13 + FastAPI
# 注：本环境 debian-slim 基础镜像拉取不稳定，改用 alpine 系（pip 轮子含 musllinux，无需编译）
FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# PyMuPDF 等带原生扩展（编译为 glibc 视角）的 wheel 在 alpine(musl) 上
# 运行时需要 libstdc++ / libgomp 动态库，否则 import fitz 报
# "Error loading shared library libstdc++.so.6"
RUN apk add --no-cache libstdc++ libgomp

# 先装依赖，利用层缓存
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 再拷贝应用代码
COPY backend/ /app/backend/

EXPOSE 8100

# 引擎版本锁定（需求 3.1）：构建时注入镜像语义版本与构建时间，
# 使 versioning.engine_version() 返回确定性版本而非回落默认 v1.0.0，
# 支撑「按历史版本重跑」时精确识别引擎版本。
ARG APP_VERSION=v1.0.0
ENV APP_VERSION=${APP_VERSION}
ENV BCR_ENGINE_VERSION=${APP_VERSION}
ENV BCR_BUILD_TIME=${BUILD_TIME:-}

# 探活：后端 /api/health 返回 200 即健康
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/api/health').status==200 else 1)"

# 由项目根以 `backend.main:app` 启动，绑定 0.0.0.0 供容器外访问
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8100"]
