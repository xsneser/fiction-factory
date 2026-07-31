# UI 改进需求记录 (2026-07-31)

## 右侧运行状态栏设计目标

### 1. 每工具单任务互斥 ✅ 已完成
- 每个工具（小说抓取 / 内容提取 / 新书生成 / 资产入库）同时只能运行一个任务
- 新任务启动时自动替代同工具的运行中任务（旧任务被取消）
- 实现：`task_manager.ensure_single(name)` — 取消并移除同 name 的运行中任务
- 内容提取改为后台线程 + SSE 流式，worker 检查 is_cancelled 真正停止旧线程

### 2. 日志统一进右侧状态栏 ✅ 已完成
- 内容提取：桥段/大纲/笑点/内涵提取阶段日志 + 统计 + 完成 → task_manager.log
- 抓取：搜索/找到/每章下载 → task_manager.log
- 入库：task_manager.log
- 右上角绿色弹框（showAlert/showToast）已全部移除
- 前端 SSE 只渲染结果卡片，不再直接写日志（消除双通道重复）

### 3. 状态栏并行显示修复 ✅ 已完成
- 不同工具任务并行显示（抓取 + 分析同时两张卡片）
- 同工具新任务替代旧任务时，旧日志自动清除（data-task-id 标记 + pollStatus 清理）
- 修复 startScout 清空全部日志的 bug
- 前端 gen 计数器：被替代的旧请求返回后不渲染结果，按钮恢复

### 4. 布局塌陷修复 ✅ 已完成（2026-07-31 09:50）
- 问题：中间主内容区过长时，整个 UI 往下坠（body 被撑高）
- 根因：body 用 min-height:100vh（内容长时 body 跟着变高）；
  .main-wrapper/main 缺 min-height:0（flex 子项默认 min-height:auto 不收缩）
- 修复：body 改 height:100vh；main-wrapper/main/aside/nav 加 min-height:0
- 效果：body=视口高度固定，main 内部滚动（scrollH>clientH），侧边栏不坠

## 改动文件
- plugins/task_manager.py — ensure_single() 单任务互斥
- ui/web_ui.py — scout_run/scout_analyze/engine_step/scout_ingest 注册任务 + 日志
- ui/templates/base.html — SPA 导航 + sessionStorage 日志持久化 + data-task-id 清理
- ui/templates/extract.html — SSE 流式 + gen 计数器 + 去掉 showAlert
- ui/templates/scout.html — 去掉 sideLog 双通道 + 去掉 showToast
