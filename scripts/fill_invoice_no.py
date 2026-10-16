# -*- coding: utf-8 -*-
"""把「发票号」写进各票索引卡（card.invoice_no）——L4 目录命名与票总览检索都用它。

口径（2026-10-16 用户要求）：
  - 发票号从「发票」类单据文件名推导，PRG 原件写法 `PRG2607-022` 归一为规范形态 `PRGINV2607-022`
  - 识别不出的票（如只到检疫证明的部分归档票）**留空**，不猜
  - 人工可在卡里直接改 `invoice_no`，本脚本不会覆盖「已存在且非空」的值

用法：
    python scripts/fill_invoice_no.py            # 只看差异
    python scripts/fill_invoice_no.py --write    # 落盘（先备份）
"""
import argparse
import datetime
import json
import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pipeline_core import INVOICE_PATTERNS, extract_invoice_no, normalize_invoice_no   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
TICKETS = ROOT / "runtime" / "tickets"
PATHS_YAML = ROOT / "config" / "paths.yaml"


def _archive_root():
    if PATHS_YAML.exists():
        for line in PATHS_YAML.read_text(encoding="utf-8").splitlines():
            m = re.match(r'\s*archive_root:\s*"([^"]+)"', line)
            if m:
                return pathlib.Path(m.group(1))
    return None


def invoice_from_redline(root, tid):
    """索引里推不出时，从红线区该票目录的文件名里找（只读，不改真源）。"""
    if not root or not root.exists():
        return ""
    for p in root.rglob("*"):
        if p.is_dir() and tid in p.name and p.parent.name in ("鳕鱼", "金线", "鱼片", "其他"):
            for f in sorted(p.rglob("*")):
                if f.is_file() and "_发票_" in f.name:
                    for pat in INVOICE_PATTERNS:
                        m = re.search(pat, f.name, re.I)
                        if m:
                            return normalize_invoice_no(m.group(0))
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rows, todo = [], []
    arch = _archive_root()
    for f in sorted(TICKETS.glob("*_index.json")):
        card = json.loads(f.read_text(encoding="utf-8"))
        tid = card.get("ticket_id", f.stem)
        old = card.get("invoice_no", "") or ""
        new = old or extract_invoice_no(card) or invoice_from_redline(arch, tid)
        rows.append((tid, card.get("friendly_label", ""), old, new))
        if new and new != old:
            todo.append((f, card, new))

    print(f"{'票ID':30s} {'友好标签':20s} {'原 invoice_no':18s} -> 新")
    print("-" * 100)
    for tid, label, old, new in rows:
        mark = "" if new else "   ⚠ 未识别（保持空）"
        print(f"{tid:30s} {label:20s} {old or '—':18s} -> {new or '—'}{mark}")

    print(f"\n需写入 {len(todo)} 张；识别不出 {sum(1 for r in rows if not r[3])} 张")
    if not args.write:
        print("[dry-run] 未写文件。确认后加 --write。")
        return 0

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = ROOT / "runtime" / f"_backup_发票号_{ts}"
    bak.mkdir(parents=True, exist_ok=True)
    for f, card, new in todo:
        shutil.copy2(f, bak / f.name)
        card["invoice_no"] = new
        card["invoice_no_source"] = "invoice_no_added_20261016"
        f.write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[备份] {bak}")
    print(f"[写入] {len(todo)} 张卡的 invoice_no")
    return 0


if __name__ == "__main__":
    sys.exit(main())
