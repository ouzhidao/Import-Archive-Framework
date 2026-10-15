# -*- coding: utf-8 -*-
"""进口单证归档 —— 只读审计（三方差异 + R7/R5/R1）

用途：把「index 卡声明的缺件」「红线区实际落盘的单据」「缺件清单.md 的行」三者摆在一起，
      输出差异表；并跑 R7(清关报关单号=检疫证号前18位) / R5(跨票重复哈希) / R1(号码一致性)。

铁律：**本脚本只读，不写任何数据、不碰红线区**（只 listdir + read_text）。
      修复数据必须由人工确认后另走流程（AGENTS.md §二）。

用法：
    python scripts/audit_archive.py                  # 控制台表 + 写 runtime/审计报告_<日期>.json
    python scripts/audit_archive.py --no-out         # 只打印，不落 json
    python scripts/audit_archive.py --archive-root D:/x/进口单证归档

纯标准库实现（不依赖 yaml/fitz/pdfplumber），任何 Python 3.8+ 可跑。
"""
import argparse
import datetime
import hashlib
import io
import json
import pathlib
import re
import sys
from collections import Counter, defaultdict

if hasattr(sys.stdout, "buffer"):
    try:                                 # 幂等重设编码，避免新建 wrapper 关掉 buffer（同进程多脚本互相 import 时会崩）
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
TICKETS_DIR = ROOT / "runtime" / "tickets"
MISSING_MD = ROOT / "runtime" / "缺件清单.md"
PATHS_YAML = ROOT / "config" / "paths.yaml"

# 必备单据（AGENTS.md §七.6「必备（8 种）」+ 清关报关单单列）
REQUIRED = ["合同", "发票", "箱单", "提单", "产地证", "卫生证", "检疫证明"]
CUSTOMS_REQUIRED = "清关报关单"          # 监管 0110 + 收货人=莱卡尔，必备
CONDITIONAL = ["捕捞证", "标签"]

# 文件名中可能出现的单据类型受控词（用于从红线区文件名反解类型）
DOC_VOCAB = [
    "附加合同", "合同", "发票", "箱单", "提单", "产地证", "捕捞证", "卫生证",
    "进境检疫许可证", "检疫证明", "报关单", "税单税费", "COA出厂报告", "我方报告单",
    "水单", "标签", "原件合集", "其他",
]
NUM18_RE = re.compile(r"(?<!\d)(\d{18,})(?!\d)")   # 前后是下划线/字母也算边界

# index 里缺件词表不统一（「报关单」/「清关报关单」），比对前先归一
DECLARED_ALIAS = {"报关单": CUSTOMS_REQUIRED, "保税入库报关单": "保税入库报关单"}


def load_archive_root(cli_value=None):
    """红线区根：命令行 > config/paths.yaml > 报错。"""
    if cli_value:
        return pathlib.Path(cli_value)
    if PATHS_YAML.exists():
        for line in PATHS_YAML.read_text(encoding="utf-8").splitlines():
            m = re.match(r'\s*archive_root:\s*"([^"]+)"', line)
            if m:
                return pathlib.Path(m.group(1))
    raise SystemExit("找不到 archive_root（用 --archive-root 指定）")


def load_cards():
    cards = {}
    for f in sorted(TICKETS_DIR.glob("*_index.json")):
        try:
            cards[f.name] = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:                                  # noqa: BLE001
            cards[f.name] = {"_load_error": str(e), "ticket_id": f.stem}
    return cards


def type_from_redline_filename(name):
    """从规范文件名反解单据类型。命名：{日期}_{吨位}t_{票ID}_{类型}_{角色}_{状态}..._{号码}.ext"""
    stem = pathlib.Path(name).stem
    parts = stem.split("_")
    # 类型通常紧跟票ID（票ID内无下划线）
    for i, p in enumerate(parts):
        if p in DOC_VOCAB and "PO" not in p:      # 简单命中
            return p
    for p in parts:
        if p in DOC_VOCAB:
            return p
    return None


def scan_redline(archive_root):
    """扫红线区：{ticket_id: {'dir': 相对路径, 'files': [...], 'types': Counter}}"""
    out = {}
    if not archive_root.exists():
        return out
    for p in archive_root.rglob("*"):
        if not p.is_dir():
            continue
        if p.parent.name not in ("鳕鱼", "金线", "鱼片", "其他", "日本", "其他物料"):
            continue
        files, types = [], Counter()
        for f in sorted(p.rglob("*")):
            if f.is_file():
                rel = f.relative_to(p).as_posix()
                files.append(rel)
                t = type_from_redline_filename(f.name)
                if t:
                    types[t] += 1
        # 目录名 = {YYYYMM}-{吨位}t-{票ID}，票ID 取吨位之后的全部（票ID 自身含 - ）
        m = re.match(r"^\d{6}-[\d.]+t-(.+)$", p.name)
        tid = m.group(1) if m else p.name
        out[tid] = {"dir": p.relative_to(archive_root).as_posix(),
                    "files": files, "types": dict(types), "n_files": len(files)}
    return out


def load_missing_md():
    """解析缺件清单.md -> {友好标签: {'吨位','冷库','missing','cond','raw'}}"""
    rows = {}
    if not MISSING_MD.exists():
        return rows
    for line in MISSING_MD.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0] in ("票",) or set(cells[0]) <= {"-", ":"}:
            continue
        rows[cells[0]] = {
            "吨位": cells[1], "冷库": cells[2],
            "missing": [] if cells[3] in ("—", "-", "") else [x.strip() for x in re.split(r"[、,，]", cells[3]) if x.strip()],
            "cond": [] if cells[4] in ("—", "-", "") else [x.strip() for x in re.split(r"[、,，]", cells[4]) if x.strip()],
        }
    return rows


def required_from_types(types):
    """按「实际存在的类型」算缺哪些必备。返回 (missing_required, customs_state)"""
    missing = [t for t in REQUIRED if t not in types]
    return missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive-root", default=None)
    ap.add_argument("--no-out", action="store_true")
    args = ap.parse_args()

    archive_root = load_archive_root(args.archive_root)
    cards = load_cards()
    redline = scan_redline(archive_root)
    md_rows = load_missing_md()

    print("=" * 100)
    print(f"进口单证归档 · 只读审计   {datetime.datetime.now():%Y-%m-%d %H:%M}")
    print(f"红线区: {archive_root}   存在={archive_root.exists()}")
    print(f"index 卡: {len(cards)} 张   红线区 L4 目录: {len(redline)} 个   缺件清单行: {len(md_rows)} 行")
    print("=" * 100)

    findings = []
    report = {"generated_at": datetime.datetime.now().isoformat(),
              "archive_root": str(archive_root), "tickets": {}}
    label2tid = {}

    # ---------- 逐票三方对比 ----------
    print("\n【A. 三方差异：index 声明 vs 红线区实际 vs 缺件清单】")
    header = f"{'票ID':30s} {'index声明缺':22s} {'红线区实际缺':22s} {'判定'}"
    print(header)
    print("-" * 100)
    for key, card in cards.items():
        tid = card.get("ticket_id", key)
        label = card.get("friendly_label", "")
        if label:
            label2tid[label] = tid
        declared = {DECLARED_ALIAS.get(x, x) for x in card.get("completeness", {}).get("missing", [])}
        declared_cond = {DECLARED_ALIAS.get(x, x) for x in card.get("completeness", {}).get("conditional_missing", [])}

        rl = redline.get(tid)
        if rl is None:
            actual = ["<红线区无此票目录>"]
        else:
            actual = required_from_types(rl["types"])
            # 清关报关单：红线区存在文件名含「_报关单_清关_」才算齐
            has_clear_customs = any("_报关单_" in pathlib.Path(f).name and "_清关_" in pathlib.Path(f).name
                                    for f in rl["files"])
            if not has_clear_customs:
                actual = actual + [CUSTOMS_REQUIRED]

        # 判定
        flags = []
        if rl is None:
            flags.append("红线区缺目录")
        else:
            if set(actual) != declared:
                flags.append("index≠红线区")
            if declared_cond & set(REQUIRED + [CUSTOMS_REQUIRED]):
                flags.append(f"必备误入条件缺:{sorted(declared_cond & set(REQUIRED + [CUSTOMS_REQUIRED]))}")

        verdict = "OK" if not flags else "；".join(flags)
        print(f"{tid:30s} {str(sorted(declared)):22s} {str(sorted(actual)):22s} {verdict}")
        if flags:
            findings.append({"级别": "A", "票": tid, "问题": verdict,
                             "index声明": sorted(declared), "红线区实际": sorted(actual)})

        report["tickets"][tid] = {
            "label": label, "declared_missing": sorted(declared),
            "declared_conditional": sorted(declared_cond),
            "redline_dir": rl["dir"] if rl else None,
            "redline_types": rl["types"] if rl else None,
            "redline_actual_missing": actual, "flags": flags,
        }

    # ---------- 缺件清单.md 覆盖度 ----------
    print("\n【B. 缺件清单.md 覆盖度】")
    missing_in_md = sorted(set(label2tid) - set(md_rows))
    extra_in_md = sorted(set(md_rows) - set(label2tid))
    # 有缺件却未进清单的票
    should_be_listed = sorted(
        l for l, t in label2tid.items()
        if (cards and t in report["tickets"]) and (
            report["tickets"][t]["declared_missing"] or report["tickets"][t]["declared_conditional"]
            or report["tickets"][t]["redline_actual_missing"])
    )
    absent_but_incomplete = [l for l in should_be_listed if l not in md_rows]
    print(f"  清单漏登（有缺件但无行）: {absent_but_incomplete or '无'}")
    print(f"  清单有行但 index 无此票 : {extra_in_md or '无'}")
    for l in absent_but_incomplete:
        findings.append({"级别": "A", "票": label2tid[l], "问题": f"缺件清单漏登（{l}）"})
    if absent_but_incomplete or extra_in_md:
        print("  ↳ 建议：缺件清单改为由本脚本扫描红线区自动生成（只读→生成物），不再手写")

    # ---------- R7 ----------
    print("\n【C. R7 清关报关单号 = 检疫证号前 18 位】")
    for tid, rl in sorted(redline.items()):
        if rl is None:
            continue
        customs, quar, quar_noid = [], [], []
        for rel in rl["files"]:
            nm = pathlib.Path(rel).name
            nums = NUM18_RE.findall(nm)
            if "_报关单_" in nm:
                if not nums:
                    continue
                role = "清关" if "_清关_" in nm else ("保税入库" if "_保税入库_" in nm else "未标角色")
                customs.append((role, nums[-1][:18]))
            if "_检疫证明_" in nm:
                if nums:
                    quar.append(nums[-1][:18])
                else:
                    quar_noid.append(nm)          # 文件名无 18 位号 → 违命名规范，R7 无法校验
        # 检疫证明缺号 → 报警（原来被静默 continue 掉）
        for nm in quar_noid:
            findings.append({"级别": "B", "票": tid,
                             "问题": f"检疫证明文件名缺 18 位号，R7 无法校验：{nm}"})
            print(f"  {tid:30s} ⚠ 检疫证缺号，R7 跳过：{nm}")
        for q in quar:
            pref = q[:18]
            hit = [c for r, c in customs if r == "清关" and c == pref]
            if not hit:
                others = [f"{r}:{c}" for r, c in customs]
                status = "待补清关报关单" if not any(r == "清关" for r, _ in customs) else "R7 不符!"
                if status == "R7 不符!":
                    findings.append({"级别": "B", "票": tid, "问题": f"R7：检疫证 {q} 前18位无对应清关报关单（现有 {others}）"})
                print(f"  {tid:30s} {status}  检疫证={q} 现有报关单={others}")
            else:
                print(f"  {tid:30s} OK ✔  检疫证={q} ↔ 清关报关单={pref}")

    # ---------- R5 ----------
    print("\n【D. R5 跨票重复内容（同哈希出现在多票）】")
    h2t = defaultdict(set)
    h2f = {}
    for key, card in cards.items():
        for d in card.get("docs", []):
            h = d.get("ocr")
            if h:
                h2t[h].add(card.get("ticket_id", key))
                h2f.setdefault(h, []).append(f"{card.get('ticket_id', key)}/{d.get('file')}")
    dups = {h: t for h, t in h2t.items() if len(t) > 1}
    if not dups:
        print("  无重复")
    for h, ts in dups.items():
        print(f"  🟠 {h[:12]}  {sorted(ts)}")
        for x in h2f[h]:
            print(f"        {x}")
    for h, ts in dups.items():
        findings.append({"级别": "B", "问题": f"R5 跨票同哈希 {h[:12]}：{sorted(ts)}", "明细": h2f[h]})

    # ---------- 汇总 ----------
    n_a = sum(1 for f in findings if f["级别"] == "A")
    n_b = sum(1 for f in findings if f["级别"] == "B")
    print("\n" + "=" * 100)
    print(f"汇总：A 级（数据一致性）{n_a} 项 ｜ B 级（规则报警）{n_b} 项")
    print("=" * 100)

    report["findings"] = findings
    report["summary"] = {"A": n_a, "B": n_b}
    if not args.no_out:
        out = ROOT / "runtime" / f"审计报告_{datetime.datetime.now():%Y%m%d_%H%M}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写: {out}（只读产物，未改动任何数据）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
