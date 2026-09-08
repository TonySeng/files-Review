# 部署到 131 服务器 —— 更新清单（final14）

生成时间：2026-09-08　基准包：final13（2026-09-07）
部署包：`bidding-review-arm64-final14.tar.gz`（698,294 字节）

---

## 一、本次更新：确定性规则体检 + 判定一致性修复

起因：规则「中标金额与暂估价数值相似度审查」连续两轮出现「detail 里算对 11.98%≥10% 应为 pass、
结构化 status 却填 fail」的自相矛盾，被系统降级为「待人工复核」。排查后定位到三类问题并全部修复。

| # | 问题 | 修复 |
|---|---|---|
| 1 | **提示词 JSON 字段顺序错误**：`status` 排在 `detail` 之前，模型必须在写理由前先吐 status，凭规则名先验填 fail 后无法回改 | `review_rule` 模板改为 `rule_id → detail → title → status`，并新增【填写顺序(强制)】指令 |
| 2 | **相对阈值无法表达**：「差值小于暂估价的 10%」被配成绝对阈值 `max_abs_diff=1000 元`，阈值不随暂估价浮动 → 漏判 | `amount_pair_diff` 新增 `max_ratio`（0.1 = 10%）+ `ratio_base`（a/b 指定分母），与 `max_abs_diff` 二选一 |
| 3 | **金额字段名非金额实体**：`b_field` 含「中标候选人」，会抽到序号/名称导致判定失真 | 字段精简为 `中标价/中标金额/投标价/预中标价` |

附带修复：
- `_extract_amount_near` 支持**单位前置**写法（「暂估价(万元) 172.7」原本会被读成 172.7 元，与「中标价 152万元」差百万倍）。前缀限长 4 字，防夹词误乘。
- 移除内置规则 `qual-credit` / `biz-price` 的 `forbid_keywords` / `require_elements`：二者是「字面子串命中即锁定 fail」，而这类禁止性表述在合规文件中恰以否定式出现（「投标人不得被列入失信被执行人名单」「不存在选择性报价」），属 100% 误判，且确定性结论 LLM 无法推翻。已回落 LLM 语义判断。

---

## 二、更新的文件清单

### 后端（3 项修改）

| 文件 | 改动要点 |
|---|---|
| `backend/services/prompt_store.py` | ①`_BUILTIN_REVIEW_RULE` 输出字段顺序改为「先 detail 后 status」+ 新增【填写顺序(强制)】四条；②法规抽取 `structured_hint` 说明支持 `max_ratio`/`ratio_base` |
| `backend/services/rules_store.py` | ①新增 `_opt_float`（区分「未配置」与「阈值就是 0」）；②`_normalize_structured` 的 `amount_pair_diff` 支持 `max_ratio`/`ratio_base`；③移除 `qual-credit`、`biz-price` 两条内置规则的误判型 structured（含原因注释） |
| `backend/services/review_engine.py` | ①新增同名 `_opt_float`；②金额对判定优先用相对阈值 `基准值 × ratio`，结论文案输出差异率与阈值百分比，比较符按 `fail_when` 渲染；③`_extract_amount_near` 新增 `_leading_unit()` 支持单位前置 |

### 前端（1 项，仅类型）

| 文件 | 改动要点 |
|---|---|
| `frontend/src/types/index.ts` | `StructuredCondition.amount_pair_diff` 增加 `max_ratio` / `ratio_base`，`max_abs_diff` 改为可选。**类型改动在编译期擦除，dist 产物与 final13 一致（`index-BjBmj2sU.js`）** |

### 脚本与文档（3 项）

| 文件 | 改动 |
|---|---|
| `migrate_structured.py` | **新增**：structured 体检 + 迁移脚本（默认预演，`--apply` 落库） |
| `make-deploy-package.sh` | `FINAL=final14`；新增把 `migrate_structured.py` 拷入部署包 |
| `transfer-to-131.sh` | 默认包改为 `bidding-review-arm64-final14.tar.gz` |

### 测试（新增 1 项，修复 1 项）

| 文件 | 说明 |
|---|---|
| `tests/test_amount_pair_ratio.py` | **新增**：31 项，覆盖相对阈值 pass/fail/R=0、白名单透传、单位前置抽取、缺值回落、绝对阈值回归 |
| `tests/test_cache_invalidation.py` | 修复导入（顶层 `from services import` 在容器内越级相对导入失败 → 改 `from backend.services import` + `/app` 入 path） |

---

## 三、部署步骤

### 方式 A：一键传输（需先填 IP）

```bash
SERVER_IP=<131服务器IP> SERVER_USER=root ./transfer-to-131.sh
```

### 方式 B：手动

```bash
scp bidding-review-arm64-final14.tar.gz <user>@<131服务器IP>:/opt/
ssh <user>@<131服务器IP>
cd /opt && tar xzf bidding-review-arm64-final14.tar.gz
cd bidding-review-deploy && ./deploy.sh
```

部署后访问：前端 `http://<IP>:8180`、后端探活 `http://<IP>:8100/api/health`。

---

## 四、部署后必须做的 4 件事

1. **⚠️ 重置提示词（最容易漏，漏了本次修复等于没做）**
   提示词一旦被持久化过，改内置模板不会自动生效。部署后必须执行一次：
   ```bash
   # 后台：提示词管理 → 规则核查提示词 → 重置为内置默认
   # 或接口：
   curl -X POST http://<IP>:8100/api/prompts/review_rule/reset -H "X-Session-Token: <admin-token>"
   ```
   若该模板此前被人工改过，重置会丢失自定义内容，需手工合并。

2. **⚠️ 执行规则迁移（131 的 app.db 不随包同步）**
   自定义规则集里的 structured 必须上机执行才会更新：
   ```bash
   docker cp bidding-review-deploy/migrate_structured.py <backend容器>:/app/
   docker exec <backend容器> python /app/migrate_structured.py            # 先看预演输出
   docker exec <backend容器> python /app/migrate_structured.py --apply    # 确认无误后写入
   ```
   脚本按规则语义（名称/描述含「暂估价」+「相似度/10%/接近」）匹配，**不依赖 rule_id**，故 131 上 id 不同也能命中；幂等。

3. **模型窗口**：若仍用本地千问 80B 等小窗口模型，按需下调 `llm_max_input_tokens`（默认 60000 按 128k 设）。

4. **前端强刷**：浏览器 `Ctrl+F5`。

---

## 五、部署后验证

```bash
curl http://<IP>:8100/api/health          # 期望 200
curl -o /dev/null -w "%{http_code}" http://<IP>:8180/   # 期望 200

# 确认暂估价规则已改（容器内）
docker exec <backend容器> python -c "
import sys,json; sys.path.insert(0,'/app')
from backend.services import rules_store
for rs in rules_store.list_rulesets():
    for r in rs.get('rules') or []:
        if '暂估价' in (r.get('name') or '') + (r.get('description') or ''):
            print(r.get('id'), r.get('name')); print(json.dumps(r.get('structured'), ensure_ascii=False))
"
# 期望输出：max_ratio=0.1, ratio_base=a, fail_when=lt；且 b_field 不含「中标候选人」
```

然后跑一次真实评标报告审核，确认该规则输出 `确定性判定通过`、detail 含「差异率 11.99%（阈值 10.00%，以 A 为分母）」，不再出现「⚠️ 模型在结构化字段判定为 fail」的降级提示。

---

## 六、回滚

`bidding-review-arm64-final13.tar.gz` 保留在项目根目录，回滚按同样步骤传入 131 解压并 `./deploy.sh`。
注意：规则数据（app.db）与提示词不在包内，回滚代码不会回退这两项——如需回退，重新执行 `migrate_structured.py` 前的备份或手工改回。
