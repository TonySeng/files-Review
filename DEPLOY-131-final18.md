# DEPLOY-131 final18 部署清单

## 本版新增：原文定位（原始文件预览 + 自动定位）

- **后端**：`_attach_locations` 为每条审核结论附加结构化定位 `locations[]`
  （`file_id / filename / ext / page / page_label / page_count / char_start / char_end /
  snippet / matched / match_type`），在 finding 事件下发前完成 → SSE、任务落库、
  `done` 事件、任务详情接口、规则下钻 `file_results.findings[].locations` 五条路径口径一致。
  `Finding`/`FileLocation` 已入 schemas；`/api/docs` 顶部新增「原文定位（Finding.locations）」
  调用说明（第三方应用对接文档）。
- **前端**：结论行「定位原文」优先走结构化定位 → 新组件 `SourcePreviewModal`：
  PDF 加载原始字节内置查看器渲染并 `#page=N` 跳页；docx 用 docx-preview 网页渲染并
  高亮滚动到锚点片段；xlsx/xls/csv 用 SheetJS 渲染、命中 sheet 自动切换并高亮单元格；
  其他类型回落提取文本分页预览（页内高亮）。无 locations 的历史任务自动回落原
  SnippetViewer 文本检索，行为不变。
- 预览依赖：`docx-preview`、`xlsx`（已进前端构建，chunk `docx-preview-*.js` / `xlsx-*.js`）。

## 部署步骤（与 final17 相同，无新增动作）

```bash
scp bidding-review-arm64-final18.tar.gz <user>@<SERVER_IP>:/opt/
ssh <user>@<SERVER_IP> 'cd /opt && tar xzf bidding-review-arm64-final18.tar.gz \
  && cd bidding-review-deploy && ./deploy.sh'
```

- 前端镜像构建会自动 `npm run build` 取最新 dist；后端改动全部在源码内，无需迁移脚本。
- **无需** `POST /api/prompts/*/reset`（本版未改内置提示词）；**无需**跑 migrate_structured.py。

## 包内容校验（打包机已做）

- 前端新 JS `index-CkO-cEl3.js` + `docx-preview-Blm67NIg.js` + `xlsx-D_0l8YDs.js` 在位
- `review_engine.py` 含 `_attach_locations`；`schemas.py` 含 `FileLocation`；openapi 文档含原文定位章节
- arm64 Dockerfile（faulthandler）/ requirements / `seccomp=unconfined` 未被覆盖
- 无 data / uploads / __pycache__ 泄露
