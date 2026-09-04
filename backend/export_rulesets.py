# -*- coding: utf-8 -*-
"""将本地审核规则集导出为「可直接导入系统」的文件。

输出两种格式，均落到 backend/data/exports/：
  1. CSV  —— 与系统导入模板完全一致（列：规则名称/类别/严重级别/规则说明/审核要点/
            需法规依据/是否启用）。直接通过前端「导入规则」或
            POST /api/rulesets/import 上传即可重新导入（无需任何后端改动）。
  2. JSON —— 全量保真（保留 id / structured 结构化条件 / builtin / mode 等）。
            需配合本仓库新增的 JSON 导入路径（rule_import.parse_rulesets_json）使用，
            可无损回灌；同时也可作为备份 / 版本管理文件。

用法：
    cd backend
    python export_rulesets.py            # 导出全部规则集
    python export_rulesets.py --name 某规则集名   # 仅导出匹配的规则集
    python export_rulesets.py --out /tmp/out     # 指定输出目录
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# 允许以脚本方式独立运行（backend 为工作目录时可直接 import services.*）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import rules_store  # noqa: E402

# 与 services/rule_import.py 的导入模板表头保持一致
CSV_HEADER = ["规则名称", "类别", "严重级别", "规则说明/条件", "审核要点", "需法规依据", "是否启用"]


def _rule_to_row(rule: dict) -> dict:
    """把一条规则对象转成 CSV 模板的一行（字典，键为中文表头）。"""
    checkpoints = rule.get("checkpoints") or []
    if isinstance(checkpoints, str):
        checkpoints = [c for c in checkpoints.split("；") if c.strip()]
    return {
        "规则名称": rule.get("name", "").strip(),
        "类别": rule.get("category", "").strip(),
        "严重级别": rule.get("severity", "").strip(),
        "规则说明/条件": (rule.get("description") or "").strip(),
        "审核要点": "；".join(str(c).strip() for c in checkpoints if str(c).strip()),
        "需法规依据": "是" if rule.get("need_legal_basis") else "否",
        "是否启用": "是" if rule.get("enabled", True) else "否",
    }


def _safe_filename(s: str) -> str:
    keep = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_", " "):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep).strip().replace(" ", "_") or "ruleset"


def export_csv(ruleset: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = out_dir / f"ruleset__{_safe_filename(ruleset['id'])}__{_safe_filename(ruleset.get('name', ''))}.csv"
    rows = [_rule_to_row(r) for r in ruleset.get("rules", [])]
    # 含 BOM（utf-8-sig），便于中文版 Excel 直接打开不出现乱码
    with fname.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return fname


def export_json(rulesets: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = out_dir / "rulesets_export.json"
    payload = {
        "schema": "bidding-review.rulesets/v1",
        "count": len(rulesets),
        "rulesets": rulesets,
    }
    fname.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return fname


def main() -> int:
    ap = argparse.ArgumentParser(description="导出本地审核规则集为可导入文件")
    ap.add_argument("--name", help="仅导出名称包含该关键字的规则集（模糊匹配）")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "exports"),
                    help="输出目录（默认 backend/data/exports）")
    args = ap.parse_args()

    out_dir = Path(args.out)
    all_sets = rules_store.list_rulesets()
    if args.name:
        picked = [s for s in all_sets if args.name in (s.get("name") or "")]
    else:
        picked = all_sets

    if not picked:
        print(f"[!] 未找到匹配的规则集（共 {len(all_sets)} 个本地规则集）。", file=sys.stderr)
        return 1

    json_path = export_json(picked, out_dir)
    csv_paths: list[Path] = []
    total_rules = 0
    for rs in picked:
        csv_paths.append(export_csv(rs, out_dir))
        total_rules += len(rs.get("rules", []))

    print(f"[✓] 共导出 {len(picked)} 个规则集、{total_rules} 条规则")
    print(f"    JSON（全量保真，可无损导入）: {json_path}")
    for p in csv_paths:
        print(f"    CSV（导入模板格式）        : {p}")
    print()
    print("导入方式：")
    print("  - CSV  : 系统「导入规则」/ POST /api/rulesets/import，立即生效，无需改后端")
    print("  - JSON : 需后端含 parse_rulesets_json（本仓库已补），同样通过 /import 上传即可无损回灌")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
