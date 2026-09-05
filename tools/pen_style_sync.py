# -*- coding: utf-8 -*-
"""笔名风格「规范 JSON 源 ⇄ 运行时」同步工具。

设计(2026-09-05 用户拍板「JSON 为源 + 回写」):
  styles/<pen_name>.json   = 唯一手写源(全量自足:meta 身份 + prefer/ban 规则 + 语言习惯 + 通用纪律),可提交。
  styles/<pen_name>.md     = 给 agent 用的风格全文,与运行时 get_pen_style().style_rules(=build_writing_prompt())
                             逐字节一致,由 sync 生成(勿手改、不加头注)。
  libraries/data/style_rules.jsonl + profiles/<id>.json = 运行时派生物(gitignored),由 sync 回写。

用法(仓库根):
  python tools/pen_style_sync.py export 枫落 [--force]     # 运行时 → 规范 JSON(自举;已存在且有差异需 --force)
  python tools/pen_style_sync.py sync   枫落 [--no-md] [--backup]  # 规范 JSON → 运行时 + 生成 styles/枫落.md
  python tools/pen_style_sync.py check  枫落               # 只读比对 JSON vs 运行时,有差异 exit 1
  pen 参数可传笔名(枫落)或 profile_id(profile_001),内部以 meta.profile_id 为写入键。

单写者·分阶段协议(写进 styles/README.md):
  - 离线手改风格 → 只改 styles/<pen>.json → 跑 sync。
  - agent/UI 增量调风格(add/delete_style_rule 循环)→ 阶段结束 export --force 固化回 JSON。
  - 不交错;export 无 --force 会先 diff,拦掉会覆盖 JSON 手改的情况。

护栏提示:
  - sync 在外部进程改 jsonl/profile json,长驻 58080 / 本 MCP 会话内存缓存**不热载**——要么重启 58080,
    要么由新 dsh 任务(新子进程冷读文件)消费。
  - 勿跑 test_all.py(它非幂等重写 style_rules.jsonl,且对已编辑数据本就不通过)。
  - sync 默认笔名(profile_001)规则会改全平台去 AI 味兜底面(空参 get_bans/get_word_map),改前可用 --backup。
"""
import io
import json
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from libraries.style_rules import StyleRule, StyleRuleLibrary  # noqa: E402
from libraries.profiles import ProfileManager  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLES_DIR = os.path.join(ROOT, "styles")
RULES_JSONL = os.path.join(ROOT, "libraries", "data", "style_rules.jsonl")

RULE_KEYS = ("id", "kind", "pattern", "desc", "severity", "replacements", "enabled")


# ─── 定位 ────────────────────────────────────────────────

def _canonical_path(spec: str):
    """按 pen 名或 profile_id 找 canonical JSON 路径。优先精确文件名,再扫 meta。"""
    exact = os.path.join(STYLES_DIR, spec + ".json")
    if os.path.exists(exact):
        return exact
    if os.path.isdir(STYLES_DIR):
        for f in os.listdir(STYLES_DIR):
            if not f.endswith(".json") or f == "README.md":
                continue
            p = os.path.join(STYLES_DIR, f)
            try:
                meta = json.load(open(p, encoding="utf-8")).get("meta", {})
            except Exception:
                continue
            if meta.get("profile_id") == spec or meta.get("pen_name") == spec:
                return p
    return exact


def _load_canonical(path: str) -> dict:
    d = json.load(open(path, encoding="utf-8"))
    meta = d.get("meta", {})
    ident = d.get("identity", {})
    return {
        "meta": meta,
        "style_fingerprint": ident.get("style_fingerprint", {}) or {},
        "tropes": ident.get("tropes", {}) or {},
        "language_hint": d.get("language_hint", "") or "",
        "discipline": list(d.get("discipline") or []),
        "description": meta.get("description", ""),
        "language": meta.get("language", "zh"),
        "pen_name": meta.get("pen_name", ""),
        "profile_id": meta.get("profile_id", ""),
        "rules": list(d.get("rules") or []),
    }


def _normalize(pid: str, rules: list, occupied: set) -> list:
    """JSON rules → StyleRule 列表;保 id,缺 id 或被占用 → 在全文件集补 {kind}_{N}。"""
    occ = set(occupied)
    out = []
    for d in rules:
        kind = str(d.get("kind", "")).strip()
        pattern = str(d.get("pattern", "")).strip()
        if kind not in ("prefer", "ban", "word"):
            sys.stderr.write(f"  [warn] 跳过未知 kind={kind!r} 的规则: {pattern[:40]}\n")
            continue
        rid = str(d.get("id", "")).strip()
        if not rid or rid in occ:
            n = 1
            while f"{kind}_{n}" in occ:
                n += 1
            if rid and rid != f"{kind}_{n}":
                sys.stderr.write(f"  [warn] 规则 id {rid} 被占用/冲突 → 重新分配 {kind}_{n}\n")
            rid = f"{kind}_{n}"
            occ.add(rid)
        occ.add(rid)
        out.append(StyleRule(
            id=rid, kind=kind, profile_id=pid, pattern=pattern,
            desc=str(d.get("desc", "") or ""),
            severity=str(d.get("severity", "warning") or "warning"),
            replacements=list(d.get("replacements") or []),
            enabled=bool(d.get("enabled", True)),
        ))
    return out


def _rule_key(r: StyleRule) -> dict:
    return {k: r.to_dict()[k] for k in RULE_KEYS}


# ─── check(只读比对) ─────────────────────────────────────

def _compare(doc: dict, pid: str, profile, rules: list) -> list:
    """canonical(已解析 doc) vs 运行时(profile/rules)。返回差异描述列表。"""
    diffs = []
    by_id = {}
    for r in rules:
        by_id[r.id] = r
    canon_by_id = {r.get("id", ""): r for r in doc["rules"] if r.get("id")}
    for rid, cd in canon_by_id.items():
        r = by_id.get(rid)
        if r is None:
            diffs.append(f"规则 {rid}({cd.get('kind')}) 在运行时缺失")
            continue
        ck = _rule_key(r)
        for k in RULE_KEYS:
            cv = cd.get(k, ck[k])
            if isinstance(cv, list):
                cv = list(cv)
            if ck[k] != cv:
                diffs.append(f"规则 {rid}.{k}: 运行时={ck[k]!r} vs JSON={cv!r}")
    for rid in set(by_id) - set(canon_by_id):
        diffs.append(f"规则 {rid} 仅运行时存在,JSON 未收录")
    if profile is None:
        diffs.append("笔名档案在运行时不存在(需 sync 重建)")
        return diffs
    if profile.style_fingerprint != doc["style_fingerprint"]:
        diffs.append("style_fingerprint 不一致")
    if profile.tropes != doc["tropes"]:
        diffs.append("tropes 不一致")
    if (profile.language_hint or "") != doc["language_hint"]:
        diffs.append("language_hint 不一致")
    if list(profile.discipline or []) != list(doc["discipline"]):
        diffs.append("discipline 不一致")
    if (profile.pen_name or "") != doc["pen_name"]:
        diffs.append(f"pen_name 不一致: {profile.pen_name!r} vs {doc['pen_name']!r}")
    if (profile.description or "") != doc["description"]:
        diffs.append("description 不一致")
    if (profile.language or "zh") != doc["language"]:
        diffs.append("language 不一致")
    return diffs


# ─── export ──────────────────────────────────────────────

def cmd_export(spec: str, force: bool = False) -> int:
    pm = ProfileManager()
    srl = StyleRuleLibrary()
    profile = pm.get(spec) or pm.get_by_name(spec)
    if profile is None:
        sys.stderr.write(f"[error] 运行时找不到笔名 {spec!r}(profiles/ 下无此档案)\n")
        return 1
    pid = profile.id
    rules = srl.rules_for(pid)
    os.makedirs(STYLES_DIR, exist_ok=True)
    path = os.path.join(STYLES_DIR, f"{profile.pen_name}.json")
    if os.path.exists(path) and not force:
        doc = _load_canonical(path)
        if doc["profile_id"] == pid:
            diffs = _compare(doc, pid, profile, rules)
            if diffs:
                sys.stderr.write(f"[error] {path} 与运行时已有差异({len(diffs)} 项),需 --force 覆盖(会丢 JSON 手改):\n")
                for d in diffs[:10]:
                    sys.stderr.write(f"  - {d}\n")
                return 1
    payload = {
        "meta": {
            "profile_id": pid, "pen_name": profile.pen_name,
            "language": profile.language or "zh",
            "description": profile.description or "",
            "exported_at": datetime.now().isoformat(timespec="seconds"),
        },
        "identity": {
            "style_fingerprint": dict(profile.style_fingerprint or {}),
            "tropes": dict(profile.tropes or {}),
        },
        "language_hint": profile.language_hint or "",
        "discipline": list(profile.discipline or []),
        "rules": [{k: v for k, v in r.to_dict().items() if k != "profile_id"} for r in rules],
    }
    json.dump(payload, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    n = len(payload["rules"])
    sys.stdout.write(f"[ok] export {profile.pen_name}({pid}) → {os.path.relpath(path, ROOT)}  ({n} 条规则)\n")
    return 0


# ─── sync ────────────────────────────────────────────────

def cmd_sync(spec: str, no_md: bool = False, backup: bool = False) -> int:
    path = _canonical_path(spec)
    if not os.path.exists(path):
        sys.stderr.write(f"[error] 找不到 {path!r}。先跑 export 自举,或确认笔名拼写。\n")
        return 1
    doc = _load_canonical(path)
    pid = doc["profile_id"]
    pen = doc["pen_name"] or os.path.splitext(os.path.basename(path))[0]
    if not pid:
        sys.stderr.write(f"[error] {path} 缺 meta.profile_id\n")
        return 1

    if backup:
        for p in (RULES_JSONL, os.path.join(ROOT, "profiles", f"{pid}.json")):
            if os.path.exists(p):
                shutil.copy2(p, p + ".bak")
        sys.stdout.write("[ok] 已备份 rules/profile 到 .bak\n")

    # ① 规则整体替换(保留其它 profile 原顺序)。占用集排除本 profile 自己的旧 id——
    # 否则本 profile 的旧 id 会被当成「被占用」而整体重排,两次 sync 在两组 id 间震荡不幂等。
    srl = StyleRuleLibrary()
    occupied = {r.id for r in srl.rules if (r.profile_id or "").strip() != pid}
    new_rules = _normalize(pid, doc["rules"], occupied)
    srl.rules = [r for r in srl.rules if (r.profile_id or "").strip() != pid] + new_rules
    srl._save()

    # ② profile upsert(合并保留运营字段)
    pm = ProfileManager()
    existed = pm.get(pid)
    carry = {}
    if existed:
        for k in ("word_print", "style_assets", "platform_accounts", "assigned_books", "created_at"):
            v = getattr(existed, k)
            if v:
                carry[k] = v
    from libraries.profiles import PenNameProfile
    profile = PenNameProfile(
        id=pid, pen_name=pen, language=doc["language"] or "zh",
        description=doc["description"] or "",
        style_fingerprint=dict(doc["style_fingerprint"] or {}),
        tropes=dict(doc["tropes"] or {}),
        language_hint=doc["language_hint"],
        discipline=list(doc["discipline"] or []),
        **carry,
    )
    pm.upsert(profile, created=existed is None)

    # ③ 用内存对象(规则/profile 均已更新)生成权威 md
    md = None
    if not no_md:
        md = profile.build_writing_prompt()
        md_path = os.path.join(STYLES_DIR, f"{pen}.md")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(md)

    n = len(new_rules)
    out = f"[ok] sync {pen}({pid}): jsonl 规则 {n} 条 | profile 已{'更新' if existed else '新建'} | "
    out += ("md 已生成" if md is not None else "--no-md 跳过 md")
    sys.stdout.write(out + "\n")
    sys.stdout.write("  ⚠ 长驻 58080/本 MCP 缓存不热载:重启 58080 或由新 dsh 任务读取方生效。\n")
    return 0


# ─── check ───────────────────────────────────────────────

def cmd_check(spec: str) -> int:
    path = _canonical_path(spec)
    if not os.path.exists(path):
        sys.stderr.write(f"[check] 无 {path!r}(尚未 export 自举)\n")
        return 1
    doc = _load_canonical(path)
    pid = doc["profile_id"]
    pm = ProfileManager()
    srl = StyleRuleLibrary()
    profile = pm.get(pid)
    rules = srl.rules_for(pid)
    diffs = _compare(doc, pid, profile, rules)
    if diffs:
        sys.stdout.write(f"[check] {doc['pen_name'] or path}: {len(diffs)} 处差异\n")
        for d in diffs[:20]:
            sys.stdout.write(f"  - {d}\n")
        return 1
    sys.stdout.write(f"[check] {doc['pen_name'] or path} 与运行时一致 ✓\n")
    return 0


# ─── main ────────────────────────────────────────────────

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = set(a for a in sys.argv[1:] if a.startswith("-"))
    if not args:
        print(__doc__)
        return 1
    cmd, spec = args[0], args[1] if len(args) > 1 else ""
    if not spec:
        print(__doc__)
        return 1
    if cmd == "export":
        return cmd_export(spec, force="--force" in flags)
    if cmd == "sync":
        return cmd_sync(spec, no_md="--no-md" in flags, backup="--backup" in flags)
    if cmd == "check":
        return cmd_check(spec)
    sys.stderr.write(f"[error] 未知命令 {cmd!r}(export/sync/check)\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
