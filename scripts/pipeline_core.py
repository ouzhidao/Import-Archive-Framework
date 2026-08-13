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
    """按受控词表匹配文件名中的单据类型。"""
    fn = filename
    for dt in doc_types:
        tid = dt.get("id", "")
        if tid and tid in fn:
            return tid
    return None


# ---------------------------------------------------------------------------
# M5 校验
# ---------------------------------------------------------------------------
def check_completeness(doc_ids: List[str], doc_types: List[Dict]) -> Dict:
    """按必须性矩阵检查齐备性。返回 missing / conditional_missing。"""
    missing = []
    conditional_missing = []
    present = set(doc_ids)
    for dt in doc_types:
        tid = dt.get("id", "")
        nec = dt.get("necessity", "视情况")
        if nec == "必备" and tid not in present:
            missing.append(tid)
        elif nec == "条件必备" and tid not in present:
            conditional_missing.append(tid)
    return {"missing": missing, "conditional_missing": conditional_missing}


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
