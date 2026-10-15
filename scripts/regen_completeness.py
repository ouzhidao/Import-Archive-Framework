# -*- coding: utf-8 -*-
"""按「红线区实际落盘」重算 index 齐备性 + 生成 缺件清单.md（生成物）

口径（2026-10-15 用户拍板）：
  - 缺件真源 = 红线区实际落盘（index / 缺件清单 降级为**生成物**）
  - 母合同 = 每票必备（PRG 母合同不再用"引用"糊弄，缺就报缺）
  - 报关单分角色：清关报关单（我方，必备）／保税报关单（无我方信息，非必须不报警）

行为：
  - 默认 --dry-run：只打印差异，不写任何文件
  - --write：先备份（runtime/_backup_缺件重算_<时间戳>/）→ 再写 index 的 completeness 与 缺件清单.md
  - 不动红线区、不动 index 的 docs 明细（只改 completeness 字段；占位条目另记）

用法：
    python scripts/regen_completeness.py            # 看差异
    python scripts/regen_completeness.py --write    # 落盘
"""
import argparse
import datetime
import io
import json
import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import audit_archive as aa          # noqa: E402  （它已在导入时把 stdout 包成 utf-8，这里不要再包一次，
                                    #   否则旧 wrapper 被 GC 会连带关掉底层 buffer）

ROOT = aa.ROOT
TICKETS_DIR = aa.TICKETS_DIR
MISSING_MD = aa.MISSING_MD

# 保税报关单（非必须，不报警）：文件名含「_报关单_保税入库_」
CUSTOMS_BONDED = "保税报关单"


def analyze(card, rl):
    """返回 (missing, cond, placeholder_files, has_clear_customs)"""
    if rl is None:
        return ["<红线区无此票目录>"], [], [], False
    types = rl["types"]
    missing = [t for t in aa.REQUIRED if t not in types]          # 含「合同」→ 母合同必备
    has_clear = any("_报关单_" in pathlib.Path(f).name and "_清关_" in pathlib.Path(f).name
                    for f in rl["files"])
    if not has_clear:
        missing.append(aa.CUSTOMS_REQUIRED)
    cond = [t for t in aa.CONDITIONAL if t not in types]
    # index 里的占位条目（无扩展名 / 形如「母合同引用26A-01」）——不该冒充单据
    ph = [d.get("file") for d in card.get("docs", [])
          if d.get("file") and not pathlib.Path(str(d["file"])).suffix]
    return sorted(missing), sorted(cond), ph, has_clear


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="落盘（默认只 dry-run）")
    args = ap.parse_args()

    archive_root = aa.load_archive_root()
    redline = aa.scan_redline(archive_root)
    cards = aa.load_cards()

    rows, changes, placeholders = [], [], []
    for fname, card in cards.items():
        tid = card.get("ticket_id", pathlib.Path(fname).stem)
        miss, cond, ph, _ = analyze(card, redline.get(tid))
        old = card.get("completeness", {})
        old_m, old_c = sorted(old.get("missing", [])), sorted(old.get("conditional_missing", []))
        if old_m != miss or old_c != cond:
            changes.append((tid, old_m, old_c, miss, cond))
        if ph:
            placeholders.append((tid, ph))
        rows.append({
            "file": fname, "tid": tid,
            "label": card.get("friendly_label", ""),
            "warehouse": card.get("warehouse", ""),
            "missing": miss, "cond": cond,
        })

    print(f"红线区: {archive_root}")
    print(f"index 卡 {len(cards)} 张；需修正 completeness 的 {len(changes)} 张\n")
    print(f"{'票ID':28s} {'原 missing':22s} -> {'新 missing':22s} {'原 cond':16s} -> {'新 cond'}")
    print("-" * 120)
    for tid, om, oc, nm, nc in changes:
        print(f"{tid:28s} {str(om):22s} -> {str(nm):22s} {str(oc):16s} -> {str(nc)}")

    if placeholders:
        print("\n占位条目（非实体文件，将删除并记入 note）:")
        for tid, ph in placeholders:
            print(f"  {tid}: {ph}")

    print(f"\n缺件清单将有 {sum(1 for r in rows if r['missing'] or r['cond'])} 行")

    if not args.write:
        print("\n[dry-run] 未写任何文件。确认后加 --write 落盘。")
        return 0

    # ---- 落盘：先备份 ----
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = ROOT / "runtime" / f"_backup_缺件重算_{ts}"
    (bak / "tickets").mkdir(parents=True, exist_ok=True)
    for f in TICKETS_DIR.glob("*_index.json"):
        shutil.copy2(f, bak / "tickets" / f.name)
    if MISSING_MD.exists():
        shutil.copy2(MISSING_MD, bak / MISSING_MD.name)
    print(f"\n[备份] {bak}")

    now = datetime.datetime.now().isoformat(timespec="seconds")
    for r in rows:
        f = TICKETS_DIR / r["file"]
        card = json.loads(f.read_text(encoding="utf-8"))
        # 删占位条目（冒充单据）
        ph = next((p for t, p in placeholders if t == r["tid"]), None)
        if ph:
            card["docs"] = [d for d in card.get("docs", []) if d.get("file") not in ph]
            note = card.setdefault("notes", [])
            if isinstance(note, list):
                note.append(f"{now[:10]} 删除占位条目 {ph}（母合同按必备处理）")
        card["completeness"] = {"missing": r["missing"], "conditional_missing": r["cond"]}
        card["completeness_source"] = "redline_scan"
        card["completeness_at"] = now
        f.write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 生成 缺件清单.md ----
    lines = [
        "# 进口单证 · 缺件清单（按吨位+冷库，方便查找）",
        "",
        f"> **本文件为生成物**（勿手改）：由 `scripts/regen_completeness.py` 以**红线区实际落盘**为准生成，{now}。",
        "> 口径：母合同＝每票必备；清关报关单（我方）＝必备；保税报关单（无我方信息）＝非必须不报警。",
        "> 已勾稽：`runtime/tickets/*_index.json` 的 completeness 与本表同源。",
        "",
        "| 票 | 吨位 | 冷库 | 缺（必备） | 条件缺 |",
        "|---|---:|---|---|---|",
    ]
    for r in sorted(rows, key=lambda x: (x["label"] or x["tid"])):
        if not r["missing"] and not r["cond"]:
            continue
        ton = ""
        m = re.search(r"-(\d+(?:\.\d+)?)t$", r["label"])
        if m:
            ton = m.group(1) + "t"
        lines.append(f"| {r['label'] or r['tid']} | {ton} | {r['warehouse'] or '—'} | "
                     f"{'、'.join(r['missing']) if r['missing'] else '—'} | "
                     f"{'、'.join(r['cond']) if r['cond'] else '—'} |")
    MISSING_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[写入] {len(changes)} 张 index 的 completeness + {MISSING_MD.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
