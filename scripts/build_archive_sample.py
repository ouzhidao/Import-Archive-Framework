# -*- coding: utf-8 -*-
"""模拟正式落盘：按 L1-L4 → 6 子文件夹 → 规范命名，复制成真实样本。
目标根: runtime/正式归档样本/进口单证归档/
源文件定位优先级：票工作区(rglob) > 拆分单证 > 新rar补件(递归) > 新zip补件(递归)
仅复制(copy)，不动源文件，不碰红线区。
"""
import sys, io, json, pathlib, shutil
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = pathlib.Path("runtime/2026_work/4票源文件")
OUT = pathlib.Path("runtime/tickets")
ROOT = pathlib.Path("runtime/正式归档样本/进口单证归档")
TID2KEY = {"PRG-SUR-LECKER-26A-01":"T1_26A01_BOS2603QD007",
           "PRG-SUR-LECKER-26A-03":"T2_26A03_CRYSTAL_ANIVA",
           "PRG-SUR-LECKER-26B-01":"T3_26B_FILLET",
           "ULKA-254903922":"T4_ULKA_254903922"}
# 额外补件目录（递归搜索）
EXTRA_SRC_DIRS = [
    pathlib.Path("runtime/2026_work/新rar补件_26A03/解压"),
    pathlib.Path("runtime/2026_work/新zip补件"),
]
TYPE_FOLDER = {
    "合同": "01_合同及附加", "附加合同": "01_合同及附加",
    "发票": "02_发票箱单", "箱单": "02_发票箱单", "发票草稿": "02_发票箱单", "发票原件": "02_发票箱单",
    "提单": "03_提单",
    "产地证": "04_官方通关单证", "捕捞证": "04_官方通关单证", "卫生证": "04_官方通关单证",
    "进境检疫许可证": "04_官方通关单证", "检疫证明": "04_官方通关单证", "报关单": "04_官方通关单证", "税单税费": "04_官方通关单证",
    "COA出厂报告": "05_质量与付款", "我方报告单": "05_质量与付款", "水单": "05_质量与付款",
    "标签": "06_标签及其他", "其他": "06_标签及其他", "原件合集": "06_标签及其他",
}

def find_src(key, fname):
    tdir = BASE / key
    # 1) 票工作区递归
    for p in tdir.rglob(fname):
        if p.is_file():
            return p
    # 2) 拆分单证
    sp = BASE / key / "拆分单证" / fname
    if sp.is_file():
        return sp
    # 3) 额外补件目录递归
    for d in EXTRA_SRC_DIRS:
        for p in d.rglob(fname):
            if p.is_file():
                return p
    return None

if ROOT.exists():
    shutil.rmtree(ROOT)
ROOT.mkdir(parents=True)
copied = 0
skipped = []
for idx in sorted(OUT.glob("*_index.json")):
    card = json.loads(idx.read_text(encoding="utf-8"))
    key = TID2KEY[card["ticket_id"]]
    date = card.get("date", "20260000")
    tid = card["ticket_id"]
    # 吨位（友好标签中提取，如 PRG-202602-47.58t -> 47.58）
    fl = card.get("friendly_label", "")
    tonnage = ""
    import re
    m = re.search(r"-(\d+(?:\.\d+)?)t$", fl)
    if m:
        tonnage = m.group(1)
    sup = "SUP_PRG" if "PRG" in card["supplier_group"] else ("SUP_ULKA" if "ULKA" in card["supplier_group"] else card["supplier_group"])
    mat = {"pollock": "鳕鱼", "itoroyi": "金线", "fillet": "鱼片"}.get(card["material"], "其他")
    # L4 带吨位：{YYYYMM-吨位}t-{正式票ID}
    l4 = f"{date[:6]}-{tonnage}t-{tid}" if tonnage else f"{date}_{tid}"
    ticket_dir = ROOT / sup / mat / l4
    for d in card["docs"]:
        dtype, status, fname = d["type"], d["status"], d["file"]
        folder = TYPE_FOLDER.get(dtype, "06_标签及其他")
        src = find_src(key, fname)
        if not src:
            skipped.append((tid, fname))
            continue
        ext = pathlib.Path(fname).suffix or ".pdf"
        # 新命名规范：{YYYYMMDD}_{吨位}t_{票ID}_{类型}_{状态}{版本}_{号码}
        # 从源文件名提取号码（发票号/提单号/报关单号/检疫证号）
        num = ""
        stem = pathlib.Path(fname).stem
        # 已知号码正则：PRGINV/SUL/BOS/CRAN/PRO/数字长串
        m_num = re.search(r"(PRGINV[\w\-]+|SUL[\w\-/]+|BOS[\w]+|CRAN[\w]+|PRO[\w]+|\d{15,})", stem)
        if m_num:
            num = m_num.group(1)
        newname = f"{date}_{tonnage}t_{tid}_{dtype}_{status}"
        if num:
            newname += f"_{num}"
        newname += ext
        target_dir = ticket_dir / folder
        target = target_dir / newname
        if target.exists():
            # 版本冲突：状态后加 2
            newname = f"{date}_{tonnage}t_{tid}_{dtype}_{status}2"
            if num:
                newname += f"_{num}"
            newname += ext
            target = target_dir / newname
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1

if skipped:
    print("跳过（找不到源）:")
    for tid, fn in skipped:
        print(f"  {tid}: {fn}")

print(f"样本重新生成: {copied} 份文件 -> {ROOT}")
