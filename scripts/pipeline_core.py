# -*- coding: utf-8 -*-
"""
管线核心：归票(M2) + 索引卡(M4) + 齐备性/防错校验(M5)
纯 Python + 文件/JSON 通信，平台无关。业务规则全读 config/*.yaml。
"""
import pathlib
import json
import re
import hashlib
import datetime
from typing import List, Dict, Optional

import yaml

# 词表映射：供应商集团 -> 中文目录名（L2）
SUP_GROUP_CN = {
    "SUP_PRG": "SUP_PRG",
    "SUP_AMERICAN_SEAFOODS": "SUP_美国海产",
    "SUP_ULKA": "SUP_ULKA",
    "SUP_LECKER_INDUSTRY": "SUP_日水",
    "SUP_THESEAFOODCOMPANY": "SUP_TheSeafoodCompany",
    "SUP_DALU": "SUP_DALU",
    "SUP_SONGTIAN": "SUP_松田",
    "SUP_CMCC": "SUP_CMCC",
    "SUP_GID": "SUP_GID",
}

# 物料词表：英文 id -> 中文目录名（L3，你已定中文）
MATERIAL_CN = {
    "pollock": "鳕鱼",
    "itoroyi": "金线",
    "fillet": "鱼片",
    "other": "其他",
}


def load_config():
    cfg_dir = pathlib.Path(__file__).resolve().parent.parent / "config"
    cfg = {}
    for name in ("doc_types", "suppliers", "routing", "rules", "paths"):
        with open(cfg_dir / f"{name}.yaml", encoding="utf-8") as f:
            cfg[name] = yaml.safe_load(f)
    return cfg


def norm_contract(contract: str) -> str:
    """合同号归一化：/ 换 -，去空格。检索时再归一化回。"""
    if not contract:
        return contract
    return contract.replace("/", "-").replace("\\", "-").strip()


# ---------------------------------------------------------------------------
# 发票号（2026-10-16 新增：L4 目录名与票总览检索都要用）
# ---------------------------------------------------------------------------
INVOICE_PATTERNS = [
    r"PRGINV\d{4}-\d{3}",          # PRGINV2607-022（规范形态）
    r"PRG\d{4}-\d{3}",             # PRG2607-022（PRG 原件上的写法，等价）
    r"SUL-\d{2}-\d{2}-\d{4}",      # SUL-25-26-1276（ULKA）
    r"SUL[A-Z0-9\-]{4,}",          # 其他 ULKA 变体
]


def normalize_invoice_no(s: str) -> str:
    """发票号归一：PRG 原件的 `PRG2607-022` → 规范形态 `PRGINV2607-022`（其余原样）。"""
    if not s:
        return ""
    s = s.strip().upper()
    m = re.fullmatch(r"PRG(\d{4}-\d{3})", s)
    if m:
        return f"PRGINV{m.group(1)}"
    return s


def extract_invoice_no(card: Dict) -> str:
    """取票的发票号：先看 card['invoice_no']（人工可改），否则从「发票」类单据文件名推导。

    只认受控样式，识别不出返回 ""（宁可空，不可乱填）。
    """
    v = normalize_invoice_no(card.get("invoice_no", "") or "")
    if v:
        return v
    for d in card.get("docs", []):
        if d.get("type") != "发票":
            continue
        name = str(d.get("file", ""))
        for p in INVOICE_PATTERNS:
            m = re.search(p, name, re.I)
            if m:
                return normalize_invoice_no(m.group(0))
    return ""


def l4_name(card: Dict) -> str:
    """L4 业务票目录名：{YYYYMM}-{吨位}t-{正式票ID}-{发票号}

    2026-10-16 起加入发票号段（便于按发票号检索；无发票号的票仅到票ID）。
    """
    tid = card.get("ticket_id", "")
    label = card.get("friendly_label", "") or ""
    m = re.search(r"-(\d{6})-(\d+(?:\.\d+)?)t$", label)
    if m:
        ym, ton = m.group(1), m.group(2)
    else:
        ym = (card.get("date", "") or "")[:6]
        ton = str(card.get("quantity_tons") or "").rstrip("0").rstrip(".")
    base = f"{ym}-{ton}t-{tid}" if (ym and ton) else tid
    inv = extract_invoice_no(card)
    return f"{base}-{inv}" if inv else base


# ---------------------------------------------------------------------------
# M2 归票：文件名优先匹配合同号/票ID
# ---------------------------------------------------------------------------
CONTRACT_RE = re.compile(r"([A-Za-z0-9]+[-/]?[A-Za-z0-9]+[-/]?[A-Za-z0-9]*-?(?:26[A-Z])?[-]?[0-9]*)")
TICKET_RE = re.compile(r"(?P<contract>[A-Z]{2,}\d{2})\s*[-]?\s*(?P<batch>\d{2})")


def detect_ticket_from_filename(filename: str, known_tickets: Optional[List[str]] = None) -> Optional[str]:
    """文件名直接含票ID/合同号 -> 高置信归票（L1）。"""
    known_tickets = known_tickets or []
    for t in known_tickets:
        if t in filename:
            return t
    m = TICKET_RE.search(filename)
    if m:
        return f"{m.group('contract')}-{m.group('batch')}"
    return None


# ---------------------------------------------------------------------------
# M4 索引卡
# ---------------------------------------------------------------------------
def new_index_card(ticket_id: str, contract: str, supplier_group: str, material: str):
    return {
        "ticket_id": ticket_id,
        "contract_no": contract,
        "batch": "01",
        "supplier_group": supplier_group,
        "material": material,
        "port": "",
        "bl_numbers": [],
        "customs_transit_nos": [],
        "vessel": "",
        "cert_numbers": {"health": "", "origin": "", "catch": ""},
        "lots": [],
        "origin_country": "",
        "producer": "",
        "quantity_tons": 0.0,
        "net_weight_kg": 0.0,
        "cartons": 0,
        "docs": [],
        "completeness": {"missing": [], "conditional_missing": []},
        "alerts": [],
        "stage": "草稿审单",
        "updated_at": datetime.datetime.now().isoformat(),
    }


def doc_type_from_filename(filename: str, doc_types: List[Dict]) -> Optional[str]:
    """按受控词表匹配文件名中的单据类型。

    2026-10-15 修：按词长**降序**匹配（否则「附加合同」会先被「合同」命中而误判），
    并优先匹配 `_类型_` 独立段，子串匹配仅作兜底。
    """
    ids = [dt.get("id", "") for dt in doc_types if dt.get("id")]
    segs = set(pathlib.Path(filename).stem.split("_"))
    for tid in sorted(ids, key=len, reverse=True):
        if tid in segs:
            return tid
    for tid in sorted(ids, key=len, reverse=True):
        if tid in filename:
            return tid
    return None


# ---------------------------------------------------------------------------
# M5 校验
# ---------------------------------------------------------------------------
# 报关单展示话术（AGENTS.md §七.6，2026-10-15 用户定）
CUSTOMS_REQUIRED = "清关报关单"    # 我方清关（监管 0110）→ 必备
CUSTOMS_BONDED = "保税报关单"      # 无我方信息的保税入库（1233）→ 非必须


def _norm_doc_id(x: str) -> str:
    """把 index/清单里的受控词归一到展示话术。"""
    if x in ("报关单", CUSTOMS_REQUIRED):
        return CUSTOMS_REQUIRED
    if x == CUSTOMS_BONDED:
        return CUSTOMS_BONDED
    return x


def check_completeness(doc_ids: List[str], doc_types: List[Dict]) -> Dict:
    """按必须性矩阵检查齐备性。返回 missing / conditional_missing / unverified。

    2026-10-15 修：报关单不再按 doc_types 里的「条件必备」处理——
      清关报关单（我方）＝必备；保税报关单（无我方信息）＝非必须、不报警。
      这里与 scripts/regen_completeness.py、scripts/audit_archive.py 保持同一口径。
    """
    present = {_norm_doc_id(x) for x in doc_ids}
    missing, conditional_missing, unverified = [], [], []
    for dt in doc_types:
        tid = dt.get("id", "")
        if tid == "报关单":
            if CUSTOMS_REQUIRED not in present:
                if "报关单" in doc_ids:          # 有单据但没标角色 → 无法判定，不硬报缺
                    unverified.append(CUSTOMS_REQUIRED)
                else:
                    missing.append(CUSTOMS_REQUIRED)
            continue
        nec = dt.get("necessity", "视情况")
        if nec == "必备" and tid not in present:
            missing.append(tid)
        elif nec == "条件必备" and tid not in present:
            conditional_missing.append(tid)
    return {"missing": missing, "conditional_missing": conditional_missing,
            "unverified": unverified}


def rule_checks(card: Dict, rules_cfg: Dict) -> List[Dict]:
    """R1-R6 防错规则（只报警不阻塞）。"""
    alerts = []
    return alerts


if __name__ == "__main__":
    cfg = load_config()
    print("config 加载 OK:", list(cfg.keys()))
    print("doc_types 数:", len(cfg["doc_types"].get("doc_types", [])))
    print("suppliers 数:", len(cfg["suppliers"].get("suppliers", [])))
    print("rules 数:", len(cfg["rules"].get("rules", [])))
