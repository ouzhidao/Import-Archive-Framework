# -*- coding: utf-8 -*-
"""L4 业务票目录改名：加「发票号」段（2026-10-16 用户要求）

    {YYYYMM}-{吨位}t-{票ID}   →   {YYYYMM}-{吨位}t-{票ID}-{发票号}
    例：202607-75.14t-PRG-SUR-LECKER-26A-06
     →  202607-75.14t-PRG-SUR-LECKER-26A-06-PRGINV2607-022

范围（两处都改，保持一致）：
  1. 红线区（真实单据）  ← 铁律「绝不改/删/移」，**必须用户确认后才可 --commit**
  2. F 盘对外成果目录

规则：
  - 无发票号的票（如只到检疫证明的部分归档票）**跳过**，保持原名
  - 只改目录名（rename），不动任何文件内容；不改 K 盘 index
  - 生成可回滚清单 runtime/_rename_manifest_l4_<ts>.json（旧名→新名），
    回滚：按清单反向 rename（脚本 --rollback <manifest>）
  - 分批：每批 ≤10 个

用法：
    python scripts/rename_l4_add_invoice.py                 # dry-run，只列映射
    python scripts/rename_l4_add_invoice.py --commit        # 执行（先写清单）
    python scripts/rename_l4_add_invoice.py --rollback runtime/_rename_manifest_l4_xxx.json
"""
import argparse
import datetime
import io
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pipeline_core import l4_name as pc_l4_name          # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
TICKETS = ROOT / "runtime" / "tickets"
PATHS_YAML = ROOT / "config" / "paths.yaml"
F_MASTER = pathlib.Path(
    r"F:/采购资料/采购部资料/采购部供方管理,订单，合同 尾牙/原料供应商/鳕鱼相关/进口原料单证整理")
BATCH = 10
MAT_DIRS = ("鳕鱼", "金线", "鱼片", "其他")


def archive_root():
    if PATHS_YAML.exists():
        for line in PATHS_YAML.read_text(encoding="utf-8").splitlines():
            m = re.match(r'\s*archive_root:\s*"([^"]+)"', line)
            if m:
                return pathlib.Path(m.group(1))
    return None


def collect_plan():
    """返回 [(root, old_dir_path, new_name, tid, invoice_no)]"""
    cards = []
    for f in sorted(TICKETS.glob("*_index.json")):
        cards.append(json.loads(f.read_text(encoding="utf-8")))

    plan = []
    for root in (archive_root(), F_MASTER):
        if not root or not root.exists():
            print(f"  ⚠ 跳过（不存在）: {root}")
            continue
        for mat in MAT_DIRS:
            for sup in sorted(root.glob(f"*/{mat}")):
                if not sup.is_dir():
                    continue
                for l4 in sorted(sup.iterdir()):
                    if not l4.is_dir():
                        continue
                    card = next((c for c in cards if c.get("ticket_id") and c["ticket_id"] in l4.name), None)
                    if not card:
                        continue
                    want = pc_l4_name(card)
                    if want == l4.name:
                        continue
                    # 已经有发票号段（幂等）→ 跳过
                    if card.get("invoice_no") and card["invoice_no"] in l4.name:
                        continue
                    plan.append((root, l4, want, card.get("ticket_id", ""), card.get("invoice_no", "")))
    return plan, cards


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--rollback", default=None)
    args = ap.parse_args()

    if args.rollback:
        mf = pathlib.Path(args.rollback)
        man = json.loads(mf.read_text(encoding="utf-8"))
        n = 0
        for it in reversed(man["items"]):
            cur, old = pathlib.Path(it["new_path"]), pathlib.Path(it["old_path"])
            if cur.exists() and not old.exists():
                cur.rename(old)
                n += 1
                print(f"  ↩ 回滚: {cur.name} → {old.name}")
            else:
                print(f"  ⚠ 跳过回滚（源/目标异常）: {cur}")
        print(f"回滚完成 {n} 项")
        return 0

    print(f"扫描根目录：\n  红线区 (config/paths.yaml archive_root)\n  F 盘   {F_MASTER}\n")
    plan, cards = collect_plan()

    no_inv = [c["ticket_id"] for c in cards if not c.get("invoice_no")]
    n_red = sum(1 for r, *_ in plan if r == archive_root())
    n_f = len(plan) - n_red
    print(f"待改名 {len(plan)} 个目录（红线区 {n_red} + F盘 {n_f}）")
    print(f"无发票号、保持原名 {len(no_inv)} 票：{no_inv}")
    ym_changed = [(old.name, new) for root, old, new, tid, inv in plan
                  if re.match(r"^\d{6}", old.name) and re.match(r"^\d{6}", new)
                  and old.name[:6] != new[:6]]
    if ym_changed:
        print(f"⚠ 其中 {len(ym_changed)} 个的年月段会变化（＝按命名规范归一到友好标签年月，非仅追加发票号）：")
        for a, b in ym_changed[:6]:
            print(f"    {a[:6]} → {b[:6]}   （{a}）")
    print()
    print(f"{'根':6s} {'旧目录名':46s} → 新目录名")
    print("-" * 140)
    for root, old, new, tid, inv in plan:
        tag = "红线区" if root == archive_root() else "F盘"
        print(f"{tag:6s} {old.name:46s} → {new}")

    if not plan:
        print("\n无需改名（可能已改过，幂等）。")
        return 0

    if not args.commit:
        print("\n[dry-run] 未改任何目录。")
        print("⚠️  红线区属铁律保护范围（绝不改/删/移）——请把上面的映射表确认后，再加 --commit 执行。")
        return 0

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    mf = ROOT / "runtime" / f"_rename_manifest_l4_{ts}.json"
    items, n_ok, n_fail = [], 0, 0
    for i, (root, old, new, tid, inv) in enumerate(plan, 1):
        if (i - 1) % BATCH == 0:
            print(f"--- 批次 {(i - 1) // BATCH + 1}（{i}~{min(i + BATCH - 1, len(plan))}）---")
        target = old.with_name(new)
        if target.exists():
            print(f"  ✗ 目标已存在，跳过: {target}")
            n_fail += 1
            continue
        try:
            old.rename(target)
            items.append({"old_path": str(old), "new_path": str(target), "tid": tid, "invoice_no": inv})
            n_ok += 1
            print(f"  ✓ [{i}] {old.name} → {new}")
        except Exception as e:
            print(f"  ✗ [{i}] {old.name}: {e}")
            n_fail += 1
    mf.write_text(json.dumps({"ts": ts, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成 {n_ok} 项，失败 {n_fail} 项 | 回滚清单: {mf}")
    print(f"回滚命令: python scripts/rename_l4_add_invoice.py --rollback {mf.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
