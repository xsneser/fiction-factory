# NovelEngine 任务管理系统规范 v1.0

> 所有耗时后端操作（小说抓取、内容提取、桥段整理、引擎写作等）统一走此规范。

## 一、核心理念

### 1.1 任务 ＝ 一个后台进程

| 概念 | 类比 Windows | 说明 |
|------|-------------|------|
| 任务卡片 | 任务管理器里的进程行 | 每个耗时操作占一个卡片 |
| 日志区 | 进程的输出/事件日志 | 所有任务的日志按时间混合排列 |
| 关闭卡片 | 关掉任务管理器窗口 | **不 kill 进程**，只是 UI 不显示了 |
| 取消任务 | 结束进程 | 需要后端显式支持，前端有停止按钮 |

### 1.2 三个基本原则

1. **任务卡片和日志是两回事**，各自独立更新
2. **关闭 ≠ 取消**。关闭只是 UI 操作，后端任务继续跑完
3. **所有耗时操作必须注册任务**。不允许有"后台偷偷跑"的未注册操作

---

## 二、数据模型

### 2.1 任务状态机

```
         start()
           │
           ▼
      ┌─────────┐     cancel()     ┌───────────┐
      │ running │ ───────────────→ │ cancelled │
      └────┬────┘                  └───────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
  done()       fail()
     │           │
     ▼           ▼
  ┌──────┐   ┌────────┐
  │ done │   │ failed │
  └──────┘   └────────┘
```

### 2.2 任务对象字段

```python
{
    "id": "fetch_十日终焉",       # 全局唯一
    "name": "小说抓取",            # 类型名：显示用
    "title": "十日终焉",           # 操作对象名
    "phase": "下载",               # 当前阶段
    "phase_display": "下载: 第5章", # 阶段详情
    "current": 5,                  # 当前进度
    "total": 30,                   # 总进度
    "status": "running",           # running | done | failed | cancelled
    "started_at": "23:51:32",      # 启动时间
    "ended_at": "23:56:47",        # 完成/失败/取消时间
    "ended_at_ts": 1234567890.0,   # 用于自动清理
    "logs": [                      # 最多100条
        {"time": "23:51:33", "message": "找到: ...", "level": "success"},
    ],
    "cancellable": True,           # 是否支持取消（按需实现）
}
```

---

## 三、后端规范

### 3.1 任务注册

每个耗时操作入口处，必须注册任务：

```python
from plugins import task_manager

def my_long_op():
    task_id = "my_op_unique_id"
    task_manager.start(task_id, name="我的操作", title="对象名", total=100)
    
    try:
        for i in range(100):
            # 在迭代/循环中更新进度
            task_manager.progress(task_id, i+1, 100, "处理中", f"第{i+1}步")
            
            # 添加日志
            task_manager.log(task_id, f"第{i+1}步完成", "info")
            
            # 检查是否被取消
            if task_manager.is_cancelled(task_id):
                task_manager.fail(task_id, "用户取消")
                return
            
            do_work_step(i)
        
        task_manager.done(task_id, "处理完成")
    
    except Exception as e:
        # 友好的错误提示
        user_msg = task_manager.translate_error(str(e))
        task_manager.fail(task_id, user_msg)
```

### 3.2 API 端点

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/status/tasks` | GET | 获取所有任务列表（pollStatus 轮询用） |
| `/api/status/tasks/close` | POST | 关闭任务卡片（仅 UI 操作，不杀进程） |
| `/api/status/tasks/cancel` | POST | 取消正在运行的任务（需后端支持） |

### 3.3 日志管理

- 日志条目按时间顺序存储
- 前端日志区增量追加，不做全量替换
- 每个任务最多保留 100 条日志
- 所有任务的日志在日志区**按时间混合排列**

---

## 四、前端规范

### 4.1 布局结构

```html
<aside id="status-bar">
  <div class="status-title">⏳ 运行状态</div>
  <div id="status-tasks">          <!-- 任务卡片区 -->
    <div class="status-card"> ... </div>  <!-- 任务A -->
    <div class="status-card"> ... </div>  <!-- 任务B -->
  </div>

  <div class="status-title" style="margin-top:12px">📋 日志</div>
  <div id="task-log-list">          <!-- 日志区，所有任务日志混合排列 -->
    <div>23:51:33 找到: 十日终焉</div>
    <div>23:51:34 下载: 第1章</div>
    <div>23:51:35 [内容提取] 分析中...</div>
  </div>
</aside>
```

### 4.2 任务卡片规则

- 每个任务一个卡片
- 卡头：任务名 + ✕ 关闭按钮
- 进度条：仅 running 状态显示
- 卡底：当前阶段 + 时间
- **关闭按钮始终存在**，但不影响后端任务
- 卡片按任务开始时间从新到旧排列

### 4.3 pollStatus 轮询逻辑

```
每2秒 GET /api/status/tasks
  → 全量替换 status-tasks 内的卡片 HTML
  → 增量追加日志到 task-log-list（按id+count追踪去重）
```

### 4.4 新增功能接入步骤

```markdown
1. 在入口函数调用 `task_manager.start()`
2. 在循环/关键步骤调用 `task_manager.progress()`
3. 在关键节点调用 `task_manager.log()`
4. 在完成/异常处调用 `task_manager.done()` / `task_manager.fail()`
5. 如果支持取消：在循环中检查 `task_manager.is_cancelled()`
```

---

## 五、现有组件清单

| 功能 | 注册任务 | 任务名 | 支持取消 | 实现文件 |
|------|---------|--------|---------|---------|
| 小说抓取 | ✅ | `fetch_{书名}` | ❌ | `ui/web_ui.py:scout_run()` |
| 小说分析 | ❌ | - | ❌ | `ui/web_ui.py:scout_analyze()` |
| 引擎写书 | ❌ | - | ❌ | `libraries/engine.py` |
| 内容提取 | ❌ | - | ❌ | 待实现 |
| 桥段整理 | ❌ | - | ❌ | 待实现 |

> ⚠️ 以上 ❌ 项就是后续要逐步接入的任务。

---

## 六、边界情况

### 6.1 关闭正在运行的任务

- 前端：卡片立即从 `#status-tasks` 移除
- 后端：任务继续跑，但不再展示进度
- `task_manager.remove()` 只是从内存字典删除，不停止线程

### 6.2 页面刷新后

- 任务卡片：重新从后端获取，展示所有还在运行/未清理的任务
- 日志：从后端重新拉取（暂未实现持久化，刷新后日志丢失）

### 6.3 日志区溢出

- 最多保留 100 条日志，超过后丢弃最旧的
- 前端日志区最大高度 400px，超出滚动的

---

## 七、常见问题

**Q: 为什么关闭按钮不真的停掉任务？**
A: 关闭 = "我不想看了"，不是"别干了"。真正要停需要后端实现取消逻辑（检查 is_cancelled）。这就像 Windows 任务管理器里关掉进程行只是关掉列表视图，要杀进程需要点"结束任务"。

**Q: 多个任务的日志混在一起怎么区分？**
A: 每个日志条目有任务 id，颜色可以区分（蓝色=抓取，绿色=分析，橙色=写作）。

**Q: 怎么知道一个任务可以取消？**
A: 后端设置 `cancellable = True`，前端才显示停止按钮。
