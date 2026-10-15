# -*- coding: utf-8 -*-
"""M7 落盘（样本）：读 runtime/tickets/*_index.json → 按 L1-L4 + 6 子目录复制成样本。

来源定位（按 card.docs[*].rel 字段，不再硬编码票清单）：
    rel 相对项目根或相对 runtime/2026_work/4票源文件 —— 依次尝试；
    再兜底在 runtime/2026_work/ 下递归同名文件。

安全：
    - 只复制（copy）源文件，绝不移动/删除源；
    - 不碰红线区；
    - 目标目录已存在时**先改名备份**（带时间戳），不再静默 rmtree。

用法：
    python scripts/build_archive_sample.py                 # 写入 runtime/正式归档样本/进口单证归档
    python scripts/build_archive_sample.py --out <目录>     # 自定义目标
    python scripts/build_archive_sample.py --dry-run       # 只列计划不落盘
"""
import argparse
import datetime
import io
import json
import pathlib
import re
import shutil
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
TICKETS = ROOT / "runtime" / "tickets"
WORK = ROOT / "runtime" / "2026_work"
DEFAULT_OUT = ROOT / "runtime" / "正式归档样本" / "进口单证归档"

SUP_DIR_CN = {
    "SUP_PRG": "SUP_PRG", "SUP_AMERICAN_SEAFOODS": "SUP_美国海产", "SUP_ULKA": "SUP_ULKA",
    "SUP_LECKER_INDUSTRY": "SUP_日水", "SUP_THESEAFOODCOMPANY": "SUP_TheSeafoodCompany",
    "SUP_DALU": "SUP_DALU", "SUP_SONGTIAN": "SUP_松田", "SUP_CMCC": "SUP_CMCC", "SUP_GID": "SUP_GID",
}
MAT_CN = {"pollock": "鳕鱼", "itoroyi": "金线", "fillet": "鱼片", "other": "其他"}

TYPE_FOLDER = {
    "合同": "01_合同及附加", "附加合同": "01_合同及附加",
    "发票": "02_发票箱单", "箱单": "02_发票箱单", "发票草稿": "02_发票箱单", "发票原件": "02_发票箱单",
    "提单": "03_提单",
    "产地证": "04_官方通关单证", "捕捞证": "04_官方通关单证", "卫生证": "04_官方通关单证",
    "进境检疫许可证": "04_官方通关单证", "检疫证明": "04_官方通关单证", "报关单": "04_官方通关单证",
    "税单税费": "04_官方通关单证",
    "COA出厂报告": "05_质量与付款", "我方报告单": "05_质量与付款", "水单": "05_质量与付款",
    "标签": "06_标签及其他", "其他": "06_标签及其他", "原件合集": "06_标签及其他",
}
NUM_RE = re.compile(r"(PRGINV[\w\-]+|SUL[\w\-/]+|BOS[\w]+|CRAN[\w]+|PRO[\w]+|\d{15,})")


def find_src(rel, fname):
    """定位源文件：rel（项目根）→ rel（runtime/2026_work/4票源文件）→ 全局递归兜底。"""
    if rel:
        rel = rel.replace("\\", "/")
        for base in (ROOT, WORK / "4票源文件", WORK):
            p = base / rel
            if p.is_file():
                return p
    # 兜底：在 runtime/2026_work 下按文件名递归（取第一个）
    if WORK.exists():
        for p in WORK.rglob(fname):
            if p.is_file():
                return p
    return None


def l4_name(card):
    """{YYYYMM-吨位}t-{票ID}；吨位取友好标签末尾与 quantity_tons 互为兜底。"""
    tid = card["ticket_id"]
    label = card.get("friendly_label", "")
    m = re.search(r"-(\d+(?:\.\d+)?)t$", label)
    ton = m.group(1) if m else (str(card.get("quantity_tons") or "").rstrip("0").rstrip(".") or "")
    ym = card.get("date", "")[:6]                       # 口径：发票日期年月（与友好标签一致）
    return f"{ym}-{ton}t-{tid}" if ton and ym else tid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out_root = pathlib.Path(args.out)

    cards = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(TICKETS.glob("*_index.json"))]

    # 安全：目标已存在 → 改名备份（可逆），不 rmtree
    if out_root.exists() and not args.dry_run:
        bak = out_root.with_name(out_root.name + f"_备份_{datetime.datetime.now():%Y%m%d_%H%M%S}")
        out_root.rename(bak)
        print(f"[备份] 原样本目录已改名为: {bak.name}")

    copied, skipped = 0, []
    for card in cards:
        tid = card["ticket_id"]
        sup = SUP_DIR_CN.get(card.get("supplier_group", ""), card.get("supplier_group", ""))
        mat = MAT_CN.get(card.get("material", ""), card.get("material", ""))
        date = card.get("date", "") or "20260000"
        ticket_dir = out_root / sup / mat / l4_name(card)
        label = card.get("friendly_label", "")
        m = re.search(r"-(\d+(?:\.\d+)?)t$", label)
        ton = m.group(1) if m else ""

        for d in card.get("docs", []):
            dtype, status, fname = d.get("type", "其他"), d.get("status", "正本"), d.get("file", "")
            if not fname:
                continue
            folder = TYPE_FOLDER.get(dtype, "06_标签及其他")
            src = find_src(d.get("rel"), fname)
            if not src:
                skipped.append((tid, fname))
                continue
            ext = pathlib.Path(fname).suffix or ".pdf"
            num_m = NUM_RE.search(pathlib.Path(fname).stem)
            num = f"_{num_m.group(1)}" if num_m else ""
            role = f"_{d['role']}" if d.get("role") else ""
            newname = f"{date}_{ton}t_{tid}_{dtype}{role}_{status}{num}{ext}"
            target = ticket_dir / folder / newname
            if target.exists():
                newname = f"{date}_{ton}t_{tid}_{dtype}{role}_{status}2{num}{ext}"
                target = ticket_dir / folder / newname
            if args.dry_run:
                print(f"  [{tid}] {folder}/{newname}   <= {src.relative_to(ROOT)}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
            copied += 1

    if skipped:
        print("\n跳过（找不到源）:")
        for tid, fn in skipped:
            print(f"  {tid}: {fn}")
    verb = "计划复制" if args.dry_run else "已复制"
    print(f"\n{verb} {copied} 份文件 -> {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
