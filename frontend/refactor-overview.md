# 前端界面重构概览（主界面工作台）

## TL;DR
对「投标文件合规审核工具」主界面做了结构与响应式重构：桌面端改为**双栏工作台**（左侧粘性配置栏 + 右侧结果区），移动端改为**全屏结果区 + 浮动按钮打开配置抽屉**；并增强了交互反馈与防误触。**所有业务逻辑、API 调用、状态与处理函数均保持不变**（仅重构 UI 结构与交互）。

## 交付状态
- 类型检查 `tsc -b`：通过
- 生产构建 `vite build`：通过（3034 模块，无错误）
- 业务逻辑改动：无

## 主要改动
### 布局 / 响应式
- 新增 `src/components/ConfigPanel.tsx`：抽出左侧「待审文件 + 规则选择 + 审核选项 + 开始审核」整栏（props 透传，逻辑不变）。
- 新增 `src/components/ResultTabs.tsx`：抽出右侧 Tabs（当前任务 / 历史任务 / 反馈与训练数据），原样透传 `tasks` 与各面板。
- `src/App.tsx`：
  - 桌面端（≥992px）：左栏 `Col lg=9` 内 `.workbench-col-left` 设为 **sticky 粘性**，内容过长时独立滚动，不遮挡结果区；右栏 `Col lg=15`。
  - 移动端（<992px）：全屏结果区 + 右下**浮动按钮(FAB)**「打开审核配置」抽屉（内含同一 ConfigPanel），操作即点即用。
  - 头部操作区可换行；<560px 隐藏标题文字，避免挤压。

### 交互人性化 / 防误触
- 「开始审核」按钮禁用时包 `Tooltip`，提示「请先选择待审文件，并至少选择一条审核规则」。
- 历史任务「取消」操作增加 `Popconfirm` 二次确认（防误触）。
- 加载（Spin）、成功（`message.success`）、错误（`message.error`）反馈保持并更显式。

### 样式（`src/index.css`）
- 新增 `.workbench-col-left`、`.fab-start`（就绪态主色 / 未就绪灰色）、`.app-body--mobile`、头部响应式规则；保留全部旧类名。

## 文件清单（新增 / 修改）
- 新增：`frontend/src/components/ConfigPanel.tsx`
- 新增：`frontend/src/components/ResultTabs.tsx`
- 修改：`frontend/src/App.tsx`（布局/响应式重写，移除已不再直接引用的组件 import）
- 修改：`frontend/src/index.css`（工作台布局与响应式样式）
- 修改：`frontend/src/components/HistoryPanel.tsx`（取消操作加二次确认）

## 用户下一步建议
1. 本地预览：`cd frontend && npm run dev`（默认 http://localhost:5173）。
2. 生产构建：`cd frontend && rm -rf dist && npm run build`（先清 dist 规避本环境 safe-delete 钩子）。
3. 部署：沿用既有 `docker compose build frontend && up -d frontend`，由 nginx 托管 `dist`。
4. 验证清单：桌面双栏粘性、移动端 FAB 抽屉、开始审核禁用提示、取消二次确认、加载/成功/错误反馈。
5. 若需进一步打磨：可把左侧超大规则列表改为可折叠分组、或为首屏加骨架屏（当前为 Spin 占位）。

## 说明
- 本次遵循项目既有约定：**本环境 TeamCreate 不可用 → 降级为单 Agent 直调模式**（主理人直做设计+实现，构建作 QA 关卡）。
- 规则管理页（RuleManagementPage 等）上轮已独立重构，本次未触碰，保持原样。
