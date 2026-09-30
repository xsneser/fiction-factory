# 笔名风格规范 JSON + agent MD

每笔名一份「全量自足」规范 JSON(`styles/<笔名>.json`)是**唯一手写源**(可提交);`styles/<笔名>.md` 是给 agent 用的风格全文(与运行时 `get_pen_style().style_rules` / `build_writing_prompt()` **逐字节一致**)。

运行时真源(`libraries/data/style_rules.jsonl` + `profiles/<id>.json`)是 **gitignored 派生物**,由脚本回写。

## 文件
- `styles/枫落.json` — 枫落(profile_001)唯一手写源:meta 身份 + prefer/ban 规则 + 语言习惯 + 通用纪律。
- `styles/枫落.md` — sync 生成的 agent 风格卡(勿手改、勿加头注,保持可 diff 校验)。

## 命令(仓库根,勿跑 test_all.py)
```
python tools/pen_style_sync.py check  枫落              # 只读比对 JSON vs 运行时,有差异 exit 1
python tools/pen_style_sync.py export 枫落 [--force]    # 运行时 → JSON(自举/阶段收口;已存在且有差异需 --force)
python tools/pen_style_sync.py sync   枫落 [--no-md] [--backup]  # JSON → 运行时 + 生成 md(幂等)
```
pen 参数可传笔名或 profile_id。

## 操作协议(单写者·分阶段)
- **离线手改风格** → 只改 `styles/<笔名>.json` → 跑 `sync`(规则/身份/语言/纪律一并回写,并重生成 md)。
- **agent/UI 增量调风格**(novel-style-match 的 add/delete_style_rule 逐轮循环,或风格库页 UI)→ 阶段结束跑 `export --force` 把成果**固化回 JSON**。
- 不交错:export 无 `--force` 会先 diff,**拦掉会覆盖 JSON 手改**的情况。

## 语义
- `language_hint`/`discipline` 空(或 `""`/`[]`)= 回退引擎默认;export 时若有值会固化成有效文本。
- 规则 `id`:JSON 里带 id 则 sync 保留;缺 id/被占用 → 自动补 `{kind}_{N}`(全文件唯一)。
- JSON **不收** `word_print`/`style_assets`(已废弃)与 `platform_accounts`/`assigned_books`/`created_at`(运营元数据)——sync 从现存 profile json 合并保留。

## 生效与回滚
- sync 改的是磁盘派生物;**长驻 58080 / 本 MCP 会话内存缓存不热载**——重启 58080,或由新 dsh 任务(新子进程冷读)消费才生效。
- `sync --backup` 会把 jsonl + profile json 备份成 `.bak`;主回滚锚点是 **git 里提交的 `styles/<笔名>.json`**(`git checkout` 该文件 → 重新 `sync` 即复原运行时)。
- 默认笔名(枫落)是空参 `get_bans`/`get_word_map` 的全局兜底,sync 改它会波及全平台去 AI 味面,谨慎 + `--backup`。
