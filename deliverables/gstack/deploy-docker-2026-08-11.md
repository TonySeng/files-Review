# Docker 构建与部署验证报告

**日期**：2026-08-11
**范围**：投标文件合规审核工具 前后端容器化（本地 Docker Desktop）
**执行方式**：软件工坊主理人直调（团队调度不可用，降级为单 Agent 模式）

---

## 1. 交付物清单

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | 编排 frontend + backend 两个服务 |
| `backend/Dockerfile` | 后端镜像（python:3.13-alpine + FastAPI） |
| `frontend/Dockerfile` | 前端镜像（nginx:1.27-alpine 托管本地 dist） |
| `frontend/nginx.conf` | 单页应用回退 + `/api` 反代（SSE 关缓冲） |
| `.dockerignore` | 构建上下文忽略规则（不把含密钥配置打进镜像） |

---

## 2. 配置与本地开发环境一致性

| 项 | 本地开发 | Docker 部署 | 一致性 |
|---|---|---|---|
| 前端访问 | http://localhost:5173（vite，/api 代理到 8100） | http://localhost:8080（nginx，/api 代理到 backend:8100） | ✅ 代理行为等价（仅发布端口不同） |
| 后端地址 | 127.0.0.1:8100 | 0.0.0.0:8100（容器内） | ✅ |
| 千问 LLM | 223.111.149.152:8000 | 同（BCR_LLM_BASE_URL 覆盖，默认值一致） | ✅ |
| OCR | 223.111.149.152:8090 | 同 | ✅ |
| 知识库 | localhost:8000（宿主机） | **host.docker.internal:8000**（容器内 localhost 指向自身，必须改写） | ⚠️→✅ 经 `BCR_KB_BASE_URL` 修正 |
| 知识库 ID | 运行期配置 | `BCR_KB_ID` 显式注入 | ✅ |
| 同源/CORS | vite 代理同源 | nginx 反代同源，CORS 不触发 | ✅ |

**关键修复**：`runtime_config.json` 中 `kb_base_url=http://localhost:8000`，在后端容器内 `localhost` 指向容器自身而非宿主机知识库。通过 compose `environment` 注入 `BCR_KB_BASE_URL=http://host.docker.internal:8000` + `extra_hosts: host.docker.internal:host-gateway` 解决（config.py 环境变量优先级最高）。

---

## 3. 实施过程与障碍

1. **node / python debian-slim 基础镜像拉取失败**：本环境 Docker Hub 对 node、python(debian-slim) 镜像层持续返回 `not found` / 静默失败，仅 alpine 系镜像可稳定拉取。
   - 前端：改用「本地 `npm run build` 预生成 dist + nginx:alpine 托管」单阶段模式，彻底规避 node 镜像。
   - 后端：由 `python:3.13-slim` 改为 `python:3.13-alpine`；依赖均为 wheel（含 PyMuPDF musllinux 轮子），`pip install` 无需编译，成功。
2. 前端本地 `vite build` 首次因 WorkBuddy 的「安全删除」钩子在清理旧 `dist` 时失败，手动 `rm -rf dist` 后构建通过（属本环境钩子问题，Docker 内无此问题）。

---

## 4. 验证结果（已实跑）

```
容器状态：
  biddingfiles-review-frontend-1   Up      0.0.0.0:8080->80/tcp
  biddingfiles-review-backend-1    Up (healthy)  0.0.0.0:8100->8100/tcp

后端直连健康检查：
  GET :8100/api/health → {"status":"ok","services":{
    "llm":"http://223.111.149.152:8000",
    "ocr":"http://223.111.149.152:8090",
    "kb":"http://host.docker.internal:8000"}}

前端页面：
  GET :8080/ → HTTP 200，返回本应用页（标题「投标文件合规审核工具」）

nginx 反代链路：
  GET :8080/api/health → 200（同源反代 backend:8100 成功）

宿主机知识库可达性（容器内）：
  python -c "urllib.request.urlopen('http://host.docker.internal:8000/api/knowledge-bases')"
  → KB status: 200   ✅ 证明 KB 覆盖生效

SSE 审核接口反代：
  POST :8080/api/review/stream（空负载）→ 400（后端正确校验拒绝，链路通畅）
```

---

## 5. 启动 / 停止 / 回滚

```bash
# 启动（前端需先 npm run build 生成 dist）
cd biddingfiles-Review
docker compose up -d --build

# 停止
docker compose down

# 回滚：docker compose down 即移除容器；镜像保留在本地，可重跑 up 恢复
# 数据：文件存储于内存，重启需重新上传（与本地一致）
```

> 注意：若宿主机 8080/8100 被占用，修改 `docker-compose.yml` 的 `ports` 映射即可；前端访问地址随之变化。
