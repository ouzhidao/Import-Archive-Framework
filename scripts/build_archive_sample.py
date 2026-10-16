# -*- coding: utf-8 -*-
"""M7 落盘（样本）：读 runtime/tickets/*_index.json → 按 L1-L4 + 6 子目录复制成样本。

2026-10-15 修复（接手审查）：
  1. 同名冲突只回退一次 → 改为自增序号（正本/正本2/正本3…），杜绝静默丢件
  2. 号码正则用 \\w（Unicode）会吞中文 → 改 ASCII 字符集
  3. L4 年月/文件日期用 card.date → 改为「红线区真实 L4 优先，其次友好标签年月」
  4. 报关单缺「角色 + 带价/不带价」段 → 从 index 字段/源文件名解析补全，缺失则告警
  5. rel 失效时全局 rglob 取第一个 → 改为限定本票范围，命中 >1 时报歧义并跳过

来源定位：card.docs[*].rel（相对项目根 或 相对 runtime/2026_work/4票源文件），再兜底本票目录。
安全：只 copy 源文件；目标目录已存在时**改名备份**（带时间戳），不做 rmtree；不碰红线区（只读 listdir）。

用法：
    python scripts/build_archive_sample.py                 # 写入 runtime/正式归档样本/进口单证归档
    python scripts/build_archive_sample.py --dry-run       # 只列计划不落盘
    python scripts/build_archive_sample.py --out <目录>
"""
import argparse
import datetime
import io
import json
import pathlib
import re
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 幂等，不会关掉 buffer
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
TICKETS = ROOT / "runtime" / "tickets"
WORK = ROOT / "runtime" / "2026_work"
DEFAULT_OUT = ROOT / "runtime" / "正式归档样本" / "进口单证归档"
PATHS_YAML = ROOT / "config" / "paths.yaml"

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
# 号码段：只用 ASCII，避免 \w 吞中文（如 BOS2603QD007报关单）；允许内部连字符（PRG2607-022）
NUM_RE = re.compile(r"(PRGINV[A-Za-z0-9\-]+|SUL[A-Za-z0-9\-/]+|BOS[A-Za-z0-9]+|CRAN[A-Za-z0-9]+|"
                    r"PRO[A-Za-z0-9]+|BOVO[A-Za-z0-9]+|PRG[A-Za-z0-9\-]*\d[A-Za-z0-9\-]*|"
                    r"[A-Z]{2,}\d{3,}[A-Z0-9\-]*|[A-Za-z]?\d{15,})")
ROLE_RE = re.compile(r"_报关单_(清关|保税入库)_")
L4_RE = re.compile(r"^\d{6}-[\d.]+t-(.+)$")
DATE8_RE = re.compile(r"^(\d{8})_")


# L4 命名统一走 pipeline_core.l4_name（与票总览同源；含发票号段）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    from pipeline_core import l4_name as _pc_l4_name
except Exception:                                     # 兜底：pipeline_core 不可用时退化为旧命名
    def _pc_l4_name(card):
        tid = card.get("ticket_id", "")
        m = re.search(r"-(\d{6})-(\d+(?:\.\d+)?)t$", card.get("friendly_label", "") or "")
        ym = m.group(1) if m else (card.get("date", "") or "")[:6]
        ton = m.group(2) if m else str(card.get("quantity_tons") or "").rstrip("0").rstrip(".")
        return f"{ym}-{ton}t-{tid}" if (ym and ton) else tid


def _load_archive_root():
    if PATHS_YAML.exists():
        for line in PATHS_YAML.read_text(encoding="utf-8").splitlines():
            m = re.match(r'\s*archive_root:\s*"([^"]+)"', line)
            if m:
                return pathlib.Path(m.group(1))
    return None


def scan_redline_l4(known_tids):
    """只读扫红线区 → {票ID: (L4目录名, 常用日期前缀)}，用于让样本与真源一致。

    2026-10-16 起用「已知票ID 出现在目录名里」匹配，兼容 L4 加发票号前后的两种命名。
    """
    out = {}
    root = _load_archive_root()
    if not root or not root.exists():
        return out
    for p in root.rglob("*"):
        if not (p.is_dir() and p.parent.name in ("鳕鱼", "金线", "鱼片", "其他")):
            continue
        tid = next((t for t in known_tids if t in p.name), None)
        if not tid:
            continue
        dates = []
        for f in p.rglob("*"):
            if f.is_file():
                dm = DATE8_RE.match(f.name)
                if dm:
                    dates.append(dm.group(1))
        common = max(set(dates), key=dates.count) if dates else ""
        out[tid] = (p.name, common)
    return out


def friendly_ym_ton(card):
    """从友好标签取 (年月, 吨位)；形如 PRG-202602-47.58t。"""
    label = card.get("friendly_label", "") or ""
    m = re.search(r"-(\d{6})-(\d+(?:\.\d+)?)t$", label)
    if m:
        return m.group(1), m.group(2)
    m2 = re.search(r"-(\d+(?:\.\d+)?)t$", label)
    ton = m2.group(1) if m2 else str(card.get("quantity_tons") or "").rstrip("0").rstrip(".")
    return (card.get("date", "") or "")[:6], ton


def find_src(rel, fname, tid):
    """定位源文件：rel → 本票目录约束 → 全局兜底（命中>1 报歧义）。返回 (path, note)"""
    if rel:
        rel = rel.replace("\\", "/")
        for base in (ROOT, WORK / "4票源文件", WORK):
            p = base / rel
            if p.is_file():
                return p, ""
    if not WORK.exists():
        return None, ""
    # 兜底 1：只在本票相关目录里找（目录名含票ID）
    scoped = [p for p in WORK.rglob(fname) if p.is_file() and tid in p.as_posix()]
    if len(scoped) == 1:
        return scoped[0], "按本票目录兜底"
    hits = [p for p in WORK.rglob(fname) if p.is_file()]
    if len(hits) == 1:
        return hits[0], "按全局唯一兜底"
    if len(hits) > 1:
        return None, f"歧义：命中 {len(hits)} 处，已跳过（{'; '.join(str(h.relative_to(ROOT)) for h in hits[:3])}）"
    return None, ""


def split_customs(status, dtype, role):
    """报关单：从 status 里拆出 带价/不带价（命名规范 §4.3 要求独立成段）。"""
    price = ""
    if dtype == "报关单":
        m = re.search(r"(带价|不带价)", status)
        if m:
            price = m.group(1)
            status = (status.replace(price, "").strip("_") or "正本")
    status = (status or "正本").replace("带价", "").strip("_") or "正本"
    return status, price


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out_root = pathlib.Path(args.out)

    cards = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(TICKETS.glob("*_index.json"))]
    real = scan_redline_l4([c["ticket_id"] for c in cards])

    if out_root.exists() and not args.dry_run:
        bak = out_root.with_name(out_root.name + f"_备份_{datetime.datetime.now():%Y%m%d_%H%M%S}")
        out_root.rename(bak)
        print(f"[备份] 原样本目录已改名为: {bak.name}")

    copied, skipped, warned = 0, [], []
    used = set()            # 本轮已占用的目标路径（dry-run 下也需去重，否则看不到冲突）
    for card in cards:
        tid = card["ticket_id"]
        sup = SUP_DIR_CN.get(card.get("supplier_group", ""), card.get("supplier_group", ""))
        mat = MAT_CN.get(card.get("material", ""), card.get("material", ""))
        ym, ton = friendly_ym_ton(card)
        l4_fallback = _pc_l4_name(card)          # {YYYYMM}-{吨位}t-{票ID}-{发票号}
        l4_real, date_real = real.get(tid, ("", ""))
        l4 = l4_real or l4_fallback
        date = date_real or card.get("date", "") or (ym + "01")
        if l4_real and l4_real != l4_fallback:
            warned.append(f"{tid}: L4 用红线区真源『{l4_real}』（推算『{l4_fallback}』）")
        ticket_dir = out_root / sup / mat / l4

        for d in card.get("docs", []):
            dtype, status, fname = d.get("type", "其他"), d.get("status", "正本"), d.get("file", "")
            if not fname:
                continue
            folder = TYPE_FOLDER.get(dtype, "06_标签及其他")
            src, note = find_src(d.get("rel"), fname, tid)
            if not src:
                skipped.append((tid, fname, note))
                continue
            ext = pathlib.Path(fname).suffix or ".pdf"
            num_m = NUM_RE.search(pathlib.Path(fname).stem)
            num = f"_{num_m.group(1)}" if num_m else ""
            role, price = "", ""
            if dtype == "报关单":
                role = d.get("role") or ""
                if not role:                                    # 从源文件名兜底
                    rm = ROLE_RE.search(src.name)
                    role = rm.group(1) if rm else ""
                if not role:
                    warned.append(f"{tid}: 报关单缺 role，输出文件名无角色段（{fname}）")
                status, price = split_customs(status, dtype, role)
            seg = f"{date}_{ton}t_{tid}_{dtype}"
            if role:
                seg += f"_{role}"
            seg += f"_{status}"
            if price:
                seg += f"_{price}"
            if num:
                seg += num
            newname = seg + ext

            # 同名冲突 → 自增序号（原来只回退一次，会静默覆盖）
            target = ticket_dir / folder / newname
            i = 2
            while target.exists() or target in used:
                newname = f"{seg}{i}{ext}"
                target = ticket_dir / folder / newname
                i += 1
            used.add(target)
            if args.dry_run:
                print(f"  [{tid}] {folder}/{newname}   <= {src.relative_to(ROOT)}"
                      + (f"   ({note})" if note else ""))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
            copied += 1

    if warned:
        print("\n告警（口径/字段问题，需人工确认）:")
        for w in warned:
            print("  ⚠", w)
    if skipped:
        print("\n跳过（找不到源/歧义）:")
        for tid, fn, note in skipped:
            print(f"  {tid}: {fn}" + (f"   —— {note}" if note else ""))
    print(f"\n{'计划复制' if args.dry_run else '已复制'} {copied} 份文件 -> {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
