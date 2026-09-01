#!/usr/bin/env python3
"""
Multimodal OCR wrapper for scanned import documents.
Reads API config from environment (NEVER hardcode secrets).
Supports OpenAI-compatible chat completions with vision.

Provider examples:
  - OpenAI:  OCR_VISION_API_BASE=https://api.openai.com/v1  OCR_VISION_MODEL=gpt-4o
  - GLM-4V:  OCR_VISION_API_BASE=https://open.bigmodel.cn/api/paas/v4  OCR_VISION_MODEL=glm-4v
  - Qwen-VL: OCR_VISION_API_BASE=<your-qwen-endpoint>  OCR_VISION_MODEL=qwen-vl-max

For Claude you need a different payload builder; this module can be extended.
"""

import os
import io
import sys
import base64
import json
import re
import hashlib
import pathlib
import tempfile
import requests
import urllib.request
import urllib.parse
from typing import List, Dict, Optional


# ---------------------------------------------------------------------------
# Load .env keys (secrets stay in .env, never in Git)
# ---------------------------------------------------------------------------
def _load_env() -> Dict[str, str]:
    env = {}
    env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


_ENV = _load_env()

# 百度 OCR（主轨）多账户主备（2026-09-01 加第三账户 C，配额用尽自动切换）
BAIDU_AK_A = _ENV.get("BAIDU_OCR_AK_A", "")
BAIDU_SK_A = _ENV.get("BAIDU_OCR_SK_A", "")
BAIDU_AK_B = _ENV.get("BAIDU_OCR_AK_B", "")
BAIDU_SK_B = _ENV.get("BAIDU_OCR_SK_B", "")
BAIDU_AK_C = _ENV.get("BAIDU_OCR_AK_C", "")
BAIDU_SK_C = _ENV.get("BAIDU_OCR_SK_C", "")
# 账户列表（A→B→C 顺序尝试，配额错误自动切下一个）
BAIDU_ACCOUNTS = [(BAIDU_AK_A, BAIDU_SK_A), (BAIDU_AK_B, BAIDU_SK_B), (BAIDU_AK_C, BAIDU_SK_C)]

# ---------------------------------------------------------------------------
# Config from environment (secrets live in .env, never in Git)
# ---------------------------------------------------------------------------
API_BASE = os.getenv("OCR_VISION_API_BASE", "").rstrip("/")
API_KEY = os.getenv("OCR_VISION_API_KEY", "")
MODEL = os.getenv("OCR_VISION_MODEL", "")
MAX_TOKENS = int(os.getenv("OCR_VISION_MAX_TOKENS", "2048"))
TEMPERATURE = float(os.getenv("OCR_VISION_TEMPERATURE", "0.1"))

# ---------------------------------------------------------------------------
# Optional PDF rendering
# ---------------------------------------------------------------------------
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except Exception:
    HAS_PDFPLUMBER = False


# ---------------------------------------------------------------------------
# Baidu OCR backend (Track A, primary)
# ---------------------------------------------------------------------------
_TOKEN_CACHE: Dict[str, tuple] = {}  # ak -> (access_token, expires_at_epoch)

# ---------------------------------------------------------------------------
# OCR 配额保护（防打爆免费额度）
# 教训：之前的项目为追求准度把图片切块，单文件调用 N 次，把 1000 张/月额度撑爆。
# 本模块硬约束：
#   1) 按"页"调用，1 页 = 1 张图 = 1 次调用，绝不切块；
#   2) max_pages 封顶（默认 3），防止多页 PDF 无限调用；
#   3) 文本层优先，有文本层不耗 OCR；
#   4) 外部缓存按文件哈希，重跑零调用。
# ---------------------------------------------------------------------------
_OCR_CALL_LOG: List[Dict] = []
_OCR_QUOTA_DAILY = 200     # 单日警戒（百度免费 1000/月，保守按日控制）
_OCR_QUOTA_MAX = 900       # 月度硬顶，到顶即拒（留余量给人工兜底）


def _log_ocr_call(image_path: str, ok: bool):
    """记录每次 OCR 调用，便于配额审计。"""
    import datetime
    _OCR_CALL_LOG.append({
        "time": datetime.datetime.now().isoformat(),
        "image": str(pathlib.Path(image_path).name),
        "ok": ok,
    })


def ocr_quota_used() -> int:
    return len(_OCR_CALL_LOG)


def ocr_quota_report() -> Dict:
    return {
        "calls_this_process": len(_OCR_CALL_LOG),
        "ok": sum(1 for c in _OCR_CALL_LOG if c["ok"]),
        "fail": sum(1 for c in _OCR_CALL_LOG if not c["ok"]),
        "daily_warn": _OCR_QUOTA_DAILY,
        "monthly_hardcap": _OCR_QUOTA_MAX,
    }


def _baidu_token(ak: str, sk: str) -> str:
    import time

    now = time.time()
    if ak in _TOKEN_CACHE:
        tok, exp = _TOKEN_CACHE[ak]
        if now < exp - 60:
            return tok
    url = "https://aip.baidubce.com/oauth/2.0/token?" + urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": ak,
        "client_secret": sk,
    })
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    tok = data["access_token"]
    _TOKEN_CACHE[ak] = (tok, now + int(data.get("expires_in", 2592000)))
    return tok


# 百度 OCR 配额/额度相关错误码（借鉴标签审核项目：只有额度错误才切备用账户）
_BAIDU_QUOTA_ERROR_CODES = {17, 18, 19}  # 17=超QPS, 18=日/月额度超限, 19=总量超限

# 百度 OCR 单图最大像素（借鉴：缩图到 3.9MP，留 100K 余量，1 次调用而非分块）
_BAIDU_MAX_PX = 3_900_000


def _resize_to_max_px(img_bytes: bytes, max_px: int = _BAIDU_MAX_PX) -> bytes:
    """若图片像素超限，等比缩图到 max_px 内。返回重编码后的图片字节。"""
    try:
        from PIL import Image
        import io as _io
    except Exception:
        return img_bytes  # 无 PIL 则原样（百度会拒绝超限图，返回错误）
    try:
        im = Image.open(_io.BytesIO(img_bytes))
        w, h = im.size
        if w * h <= max_px:
            return img_bytes
        import math
        ratio = math.sqrt(max_px / (w * h))
        new_w, new_h = int(w * ratio), int(h * ratio)
        im = im.convert("RGB")
        im = im.resize((new_w, new_h), Image.LANCZOS)
        out = _io.BytesIO()
        im.save(out, format="JPEG", quality=90)
        return out.getvalue()
    except Exception:
        return img_bytes


def _preprocess_image(img_bytes: bytes) -> bytes:
    """图片预处理（借鉴标签审核项目审核方法论.md）。

    提升扫描件/深色背景/低对比度单证的 OCR 识别率：
      - 灰度化 + 对比度增强（1.5x）
      - 锐化（1.3x）
      - 若背景深（暗色扫描），反转对比度使文字变暗底浅
    仅当图片看起来是"照片/扫描件"时启用；纯文本PDF的渲染图保持原样。
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter
        import io as _io
    except Exception:
        return img_bytes
    try:
        im = Image.open(_io.BytesIO(img_bytes)).convert("L")  # 灰度
        # 对比度增强
        im = ImageEnhance.Contrast(im).enhance(1.5)
        # 锐化
        im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=130, threshold=3))
        # 深色背景检测：若平均亮度低（<100），反转
        import statistics
        px = list(im.getdata())
        avg = sum(px) / len(px)
        if avg < 100:
            im = im.point(lambda p: 255 - p)  # 反转
        out = _io.BytesIO()
        im.save(out, format="JPEG", quality=90)
        return out.getvalue()
    except Exception:
        return img_bytes


def baidu_ocr_image(image_path: str, preprocess: bool = False) -> Dict:
    """百度通用文字识别(高精度 accurate)。带 A/B 主备降级 + 配额保护 + 缩图。

    借鉴标签审核项目：
      - 缩图到 3.9MP 再 OCR，1 次调用而非分块（省 15 倍配额）；
      - 只有配额/额度错误(error_code 17/18/19)才切换备用账户；
      - 宁报 OCR 失败也不给错数据（由质检层把关）。
      - preprocess=True 时对扫描件启用图片预处理（对比度/锐化/灰度反转）。
    """
    if ocr_quota_used() >= _OCR_QUOTA_MAX:
        return {"source": "baidu", "words": [], "raw_text": "", "confidence": 0.0,
                "error": "OCR 月度配额硬顶已到，跳过（请在 runtime/ocr_quota.json 查看）"}
    last = None
    for ak, sk in BAIDU_ACCOUNTS:
        if not ak or not sk:
            continue
        try:
            token = _baidu_token(ak, sk)
            with open(image_path, "rb") as f:
                raw = f.read()
            img = _resize_to_max_px(raw)  # 缩图防超限 + 省配额
            if preprocess:
                img = _preprocess_image(img)  # 扫描件预处理（对比度/锐化/反转）
            b64 = base64.b64encode(img).decode("ascii")
            url = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate?" + urllib.parse.urlencode(
                {"access_token": token}
            )
            data = urllib.parse.urlencode(
                {"image": b64, "detect_direction": "true", "paragraph": "true"}
            ).encode("ascii")
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read().decode("utf-8"))
            if "error_code" in res:
                ec = res["error_code"]
                if ec in _BAIDU_QUOTA_ERROR_CODES:
                    # 配额错误：切备用账户
                    raise _QuotaError(f"baidu quota error {ec}: {res.get('error_msg')}")
                raise RuntimeError(f"baidu error {ec}: {res.get('error_msg')}")
            words = [w.get("words", "") for w in res.get("words_result", [])]
            _log_ocr_call(image_path, True)
            return {
                "source": "baidu",
                "words": words,
                "raw_text": "\n".join(words),
                "confidence": 0.9,
                "error": None,
            }
        except _QuotaError as e:
            last = str(e)
            _log_ocr_call(image_path, False)
            # 配额错误继续尝试备用账户
        except Exception as e:
            last = str(e)
            _log_ocr_call(image_path, False)
            # 非配额错误：不切账户（避免无意义重试浪费配额）
            return {"source": "baidu", "words": [], "raw_text": "", "confidence": 0.0,
                    "error": f"非配额错误，不切换账户: {last}"}
    return {"source": "baidu", "words": [], "raw_text": "", "confidence": 0.0,
            "error": last or "baidu ocr failed"}


class _QuotaError(Exception):
    """百度配额/额度错误，用于触发备用账户切换。"""
    pass


def extract_text_layer(pdf_path: str, max_pages: Optional[int] = None) -> Dict:
    """Extract raw text from PDF text layer (if any). Returns page texts."""
    try:
        import pdfplumber
    except Exception:
        return {"source": "none", "pages": [], "raw_text": "", "confidence": 0.0,
                "error": "no pdfplumber"}
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            if max_pages is not None and i >= max_pages:
                break
            t = (page.extract_text() or "").strip()
            pages.append(t)
    total = sum(len(p) for p in pages)
    if total > 50:
        return {"source": "text", "pages": pages, "raw_text": "\n".join(pages),
                "confidence": 1.0, "error": None}
    return {"source": "none", "pages": [], "raw_text": "", "confidence": 0.0, "error": None}


def extract_document(pdf_path: str, max_pages: Optional[int] = None, preprocess_scan: bool = True) -> Dict:
    """统一入口：文本层优先，无文本层走百度 OCR。

    preprocess_scan=True（默认）：扫描件（无文本层）自动启用图片预处理，
    提升低对比度/深色背景单证的识别率。
    """
    pdf_path = str(pdf_path)
    ext = pathlib.Path(pdf_path).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".bmp"):
        res = baidu_ocr_image(pdf_path, preprocess=preprocess_scan)
        res["pages"] = [res.get("raw_text", "")]
        return res
    text = extract_text_layer(pdf_path, max_pages=max_pages)
    if text["source"] == "text":
        return text
    try:
        imgs = pdf_pages_to_images(pdf_path, max_pages=max_pages)
    except Exception as e:
        return {"source": "none", "pages": [], "raw_text": "", "confidence": 0.0,
                "error": f"render failed: {e}"}
    if not imgs:
        return {"source": "none", "pages": [], "raw_text": "", "confidence": 0.0,
                "error": "no pages rendered"}
    all_pages = []
    for im in imgs:
        r = baidu_ocr_image(im, preprocess=preprocess_scan)
        all_pages.append(r.get("raw_text", ""))
    return {
        "source": "baidu",
        "pages": all_pages,
        "raw_text": "\n".join(all_pages),
        "confidence": 0.9,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def image_to_data_url(path: str) -> str:
    """Encode a local image file as a data URL for vision APIs."""
    ext = pathlib.Path(path).suffix.lower()
    mime = (
        "image/png"
        if ext == ".png"
        else "image/jpeg"
        if ext in (".jpg", ".jpeg")
        else "image/png"
    )
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"


def pdf_pages_to_images(
    pdf_path: str, dpi: int = 170, max_pages: Optional[int] = None
) -> List[str]:
    """Render PDF pages to PNG files. Returns list of temp image paths."""
    if not HAS_PDFPLUMBER:
        raise RuntimeError("pdfplumber is required for PDF rendering")

    out_dir = pathlib.Path(tempfile.gettempdir()) / "ocr_vision_pages"
    out_dir.mkdir(exist_ok=True)
    file_hash = hashlib.md5(str(pdf_path).encode()).hexdigest()[:12]

    paths = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            if max_pages is not None and i >= max_pages:
                break
            im = page.to_image(resolution=dpi)
            p = out_dir / f"{file_hash}_{i}.png"
            im.save(str(p))
            paths.append(str(p))
    return paths


def build_prompt(doc_type: str, schema: Dict[str, str], extra_hint: Optional[str] = None) -> str:
    """Build a strict JSON-only extraction prompt."""
    fields = "\n".join(f'  "{k}": "{v}"' for k, v in schema.items())
    hint = f"\nAdditional context/hint: {extra_hint}\n" if extra_hint else ""

    return (
        "You are an expert in reading Chinese import/export documents for frozen seafood. "
        f"Document type: {doc_type}.{hint}\n"
        "Extract the following fields. For each field, return the value and a confidence score (0.0-1.0). "
        "If a field is not visible, unreadable, or not applicable, return null and confidence 0.0. "
        "Return ONLY a JSON object with this exact structure:\n"
        '{\n'
        '  "fields": {\n'
        '    "field_name": {"value": ..., "confidence": 0.95}\n'
        '  },\n'
        '  "summary": "brief Chinese summary of what you see"\n'
        '}\n'
        "Fields to extract:\n"
        f"{fields}\n"
        "Do not output markdown code fences."
    )


def strip_code_fences(text: str) -> str:
    """Remove ```json ... ``` wrappers if the model returns them."""
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Core extraction API
# ---------------------------------------------------------------------------
def extract_from_image(
    image_path: str,
    doc_type: str,
    schema: Dict[str, str],
    extra_hint: Optional[str] = None,
) -> Dict:
    """Extract fields from a single image."""
    if not API_BASE or not API_KEY or not MODEL:
        raise RuntimeError(
            "OCR_VISION_API_BASE, OCR_VISION_API_KEY, OCR_VISION_MODEL must be set. "
            "See .env.example."
        )

    data_url = image_to_data_url(image_path)
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You read scanned import documents and return structured JSON only.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_prompt(doc_type, schema, extra_hint)},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    resp = requests.post(
        f"{API_BASE}/chat/completions",
        json=payload,
        headers=headers,
        timeout=120,
    )
    resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"]
    content = strip_code_fences(content)

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"raw": content, "parse_error": True}


def extract_from_pdf(
    pdf_path: str,
    doc_type: str,
    schema: Dict[str, str],
    extra_hint: Optional[str] = None,
    max_pages: Optional[int] = None,
) -> List[Dict]:
    """Extract fields from each page of a PDF."""
    pages = pdf_pages_to_images(pdf_path, max_pages=max_pages)
    return [extract_from_image(p, doc_type, schema, extra_hint) for p in pages]


def low_confidence_fields(result: Dict, threshold: float = 0.7) -> List[str]:
    """Return field names whose confidence is below threshold."""
    if "fields" not in result:
        return []
    return [
        k
        for k, v in result["fields"].items()
        if isinstance(v, dict) and v.get("confidence", 0) < threshold
    ]


# ---------------------------------------------------------------------------
# Default field schema for import seafood documents
# ---------------------------------------------------------------------------
def default_schema() -> Dict[str, str]:
    return {
        "document_type": "单据类型",
        "contract_no": "合同号",
        "invoice_no": "发票号",
        "bl_no": "提单号(B/L No.)",
        "vessel": "船名航次",
        "loading_port": "装货港",
        "discharge_port": "卸货港/目的港",
        "product": "品名",
        "quantity_cases": "件数/箱数",
        "net_weight_kg": "净重(kg)",
        "gross_weight_kg": "毛重(kg)",
        "origin_country": "原产国",
        "departure_country": "输出/启运国",
        "production_factory": "生产厂家",
        "production_batch": "生产日期/批号",
        "cert_no": "证书编号",
        "consignee": "收货人",
        "shipper": "发货人",
    }


# ---------------------------------------------------------------------------
# 分块 OCR（备用触发，默认不分块）
# 借鉴标签审核项目：只有缩图质检不过/编号位数异常才回退分块。
# 保完整性核心：块间 10% 重叠 + 合并去重，避免边界文字被截断。
# ---------------------------------------------------------------------------
def tile_split(img_path: str) -> List[Dict]:
    """将图片分块并逐块 OCR。返回 [{'tile': (r,c), 'raw_text': ...}, ...]。

    分块参数：
      - 单块 ≤ 3.9MP（百度上限 4MP 留余量）
      - 块间 10% 重叠（保边界完整性）
      - 护栏 cols×rows ≤ 50，超则中断（防 660 块 bug）
    """
    try:
        from PIL import Image
        import io as _io
        import math
    except Exception as e:
        return [{"tile": (0, 0), "raw_text": "", "error": f"tile_split needs PIL: {e}"}]

    im = Image.open(img_path).convert("RGB")
    w, h = im.size
    MAX_PX = 3_900_000
    aspect = w / h
    tile_h = int(math.sqrt(MAX_PX / aspect))
    tile_w = int(MAX_PX // tile_h)
    if tile_w <= 0 or tile_h <= 0:
        return [{"tile": (0, 0), "raw_text": "", "error": "tile size invalid"}]

    # 有效步进（含 10% 重叠）
    step_w = max(1, int(tile_w * 0.9))
    step_h = max(1, int(tile_h * 0.9))
    cols = math.ceil(w / step_w)
    rows = math.ceil(h / step_h)

    # 护栏：分块数异常直接中断（防配额浪费）
    if cols * rows > 50:
        return [{"tile": (0, 0), "raw_text": "", "error": f"分块数异常 {cols}x{rows}={cols*rows}>50，中断防配额浪费"}]

    results = []
    import tempfile
    tmp = pathlib.Path(tempfile.gettempdir()) / "ocr_tile"
    tmp.mkdir(exist_ok=True)
    for r in range(rows):
        for c in range(cols):
            left = c * step_w
            top = r * step_h
            right = min(left + tile_w, w)
            bottom = min(top + tile_h, h)
            box = (left, top, right, bottom)
            tile = im.crop(box)
            p = tmp / f"tile_{r}_{c}.png"
            tile.save(str(p))
            res = baidu_ocr_image(str(p))
            results.append({"tile": (r, c), "raw_text": res.get("raw_text", ""), "error": res.get("error")})
    return results


def _merge_tile_text(tiles: List[Dict]) -> str:
    """合并分块结果，按行去重（重叠区只留一份）。"""
    lines = []
    seen = set()
    for t in tiles:
        for line in (t.get("raw_text", "") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 页级索引器（M3 增强）—— 防"多页PDF只OCR第一页漏单证"
# 分层策略：先零成本扫页结构，再按需 OCR，控制成本。
# ---------------------------------------------------------------------------
# 单证标题特征词（用于页级类型识别，正则匹配）
DOC_TITLE_MARKERS = [
    ("报关单", ["中华人民共和国海关进口货物报关单", "进口货物报关单", "预录入编号"]),
    ("检疫证明", ["入境货物检验检疫证明", "入境检验检疫"]),
    ("税单", ["税费单详细信息", "税收缴款书", "税种"]),
    ("发票", ["COMMERCIAL INVOICE", "PROFORMA INVOICE", "TAX INVOICE", "INVOICE"]),
    ("箱单", ["PACKING LIST"]),
    ("提单", ["BILL OF LADING", "BILL OF LADING FOR OCEAN", "CONGENBILL"]),
    ("产地证", ["CERTIFICATE OF ORIGIN", "ORIGINAL N"]),
    ("卫生证", ["HEALTH CERTIFICATE", "INSPECTION AND QUARANTINE CERTIFICATE ISSUED"]),
    ("捕捞证", ["CATCH CERTIFICATE", "CAUGHT AND FROZEN"]),
    ("合同", ["SALES CONTRACT", "SURIMI PURCHASING CONTRACT"]),
    ("附加合同", ["ADDITIONAL AGREEMENT", "ADDENDUM"]),
    ("入仓/核注", ["INTAKE REPORT", "保税核注清单"]),
    ("标签", ["LABEL", "OUTER"]),
]

# 合并件提示词：文件名含这些词 -> 很可能是多单证合并，需全页扫描
MERGE_HINT_KEYWORDS = ["全套", "资料", "docu", "DOCU", "all", "All", "package", "包", "合并", "combined"]

# 页索引结构
# {
#   "file": ...,
#   "total_pages": N,
#   "text_pages": [0,1,...],      # 有文本层的页
#   "scan_pages": [2,3,...],      # 无文本层(扫描件)页
#   "page_types": {page_idx: "报关单"},  # 每页识别类型
#   "is_merge": bool,             # 是否多单证合并件
#   "ocr_needed": [pages],        # 需要 OCR 的页
# }


def _detect_page_type(text: str) -> Optional[str]:
    """从一页文本识别单证类型。文本层直接读，OCR后也调用。"""
    t = text.strip().upper() if text else ""
    if not t:
        return None
    for doc_type, markers in DOC_TITLE_MARKERS:
        for m in markers:
            if m.upper() in t:
                return doc_type
    return None


def build_page_index(pdf_path: str, max_pages: Optional[int] = None) -> Dict:
    """建页级索引：零成本扫页结构，标记需OCR页。

    策略：
      - 有文本层页 -> 直接读文本识别类型（0 成本）
      - 无文本层页 -> 标记需 OCR
      - 判定是否合并件（文件名关键词 + 多类型）
      - 超长保护：>30页时抽样决策
    """
    pdf_path = str(pdf_path)
    idx = {
        "file": pathlib.Path(pdf_path).name,
        "total_pages": 0,
        "text_pages": [],
        "scan_pages": [],
        "page_types": {},
        "is_merge": False,
        "ocr_needed": [],
        "strategy": "unknown",
    }
    # 文件名合并件提示
    fname = pathlib.Path(pdf_path).name
    name_hint = any(k in fname for k in MERGE_HINT_KEYWORDS)

    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            n = len(pdf.pages)
            if max_pages:
                n = min(n, max_pages)
            idx["total_pages"] = n
            for i in range(n):
                page = pdf.pages[i]
                t = (page.extract_text() or "").strip()
                if len(t) > 20:
                    idx["text_pages"].append(i)
                    dt = _detect_page_type(t)
                    if dt:
                        idx["page_types"][i] = dt
                else:
                    idx["scan_pages"].append(i)
    except Exception:
        # pdfplumber 失败，用 fitz
        try:
            import fitz
            doc = fitz.open(pdf_path)
            n = len(doc)
            if max_pages:
                n = min(n, max_pages)
            idx["total_pages"] = n
            for i in range(n):
                t = (doc[i].get_text() or "").strip()
                if len(t) > 20:
                    idx["text_pages"].append(i)
                    dt = _detect_page_type(t)
                    if dt:
                        idx["page_types"][i] = dt
                else:
                    idx["scan_pages"].append(i)
            doc.close()
        except Exception as e:
            idx["error"] = str(e)
            return idx

    # 判定合并件：文件名提示 或 页类型>=2种 或 页数>=3
    types_set = set(idx["page_types"].values())
    idx["is_merge"] = name_hint or len(types_set) >= 2 or idx["total_pages"] >= 3

    # OCR 决策
    if idx["total_pages"] == 0:
        idx["strategy"] = "empty"
    elif not idx["scan_pages"]:
        # 全文本层 -> 0 OCR
        idx["strategy"] = "text_only"
        idx["ocr_needed"] = []
    elif idx["total_pages"] > 30:
        # 超长保护：抽样
        sample = set()
        if idx["scan_pages"]:
            sample.add(idx["scan_pages"][0])  # 首个扫描页
            sample.add(idx["scan_pages"][-1])  # 末个扫描页
            mid = idx["scan_pages"][len(idx["scan_pages"]) // 2]
            sample.add(mid)
        idx["strategy"] = "sampled_ocr"
        idx["ocr_needed"] = sorted(sample)
    elif idx["is_merge"] or len(idx["scan_pages"]) >= 3:
        # 合并件/多扫描页 -> 全页 OCR（必要，防漏单证）
        idx["strategy"] = "full_ocr"
        idx["ocr_needed"] = sorted(idx["scan_pages"])
    else:
        # 简单单证 -> 只 OCR 扫描页（通常1-2页）
        idx["strategy"] = "simple_ocr"
        idx["ocr_needed"] = sorted(idx["scan_pages"])

    return idx


def ocr_pages_with_index(pdf_path: str, max_pages: Optional[int] = None) -> Dict:
    """页级索引 + 按需OCR。返回合并后的页类型 + 文本。

    防止"只OCR第1页漏单证"。文本层页免费读，扫描页按策略OCR。
    """
    idx = build_page_index(pdf_path, max_pages)
    # 对需要 OCR 的页渲染并 OCR
    results = []
    if idx["ocr_needed"]:
        imgs = pdf_pages_to_images(pdf_path, max_pages=max_pages)
        for pi in idx["ocr_needed"]:
            if pi < len(imgs):
                r = baidu_ocr_image(imgs[pi])
                txt = r.get("raw_text", "")
                dt = _detect_page_type(txt)
                if dt:
                    idx["page_types"][pi] = dt
                results.append({"page": pi, "source": "ocr", "type": dt, "text": txt[:300]})
    # 文本层页
    for pi in idx["text_pages"]:
        # 重新读文本
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                t = (pdf.pages[pi].extract_text() or "").strip()
        except Exception:
            t = ""
        results.append({"page": pi, "source": "text", "type": idx["page_types"].get(pi), "text": t[:300]})

    idx["results"] = sorted(results, key=lambda x: x["page"])
    return idx


# ---------------------------------------------------------------------------
# OCR 质检层（借鉴标签审核项目"四道质检"，宁报失败不给错数据）
# ---------------------------------------------------------------------------
def ocr_quality_check(result: Dict, doc_type_hint: str = "") -> Dict:
    """对 OCR 结果做质检。返回带 qc 标记的结果。

    qc_flags: list of 问题标记，空=通过
      resolution_low   : 分辨率不足（<1500px 最长边）
      no_key_fields    : 关键字段缺失（按单据类型）
      no_text          : 无有效文本
    标注"待人工"，不丢弃——宁进人工队列不给错数据。
    """
    flags = []
    text = result.get("raw_text", "") or ""
    # 检测3：无有效文本
    if len(text.strip()) < 20:
        flags.append("no_text")
    # 关键字段完整性（按单据类型）
    if doc_type_hint == "报关单":
        if "报关单号" not in text and "预录入编号" not in text and "海关编号" not in text:
            flags.append("no_key_fields")
    elif doc_type_hint == "检疫证明":
        if "检疫" not in text:
            flags.append("no_key_fields")
    elif doc_type_hint == "发票":
        if "invoice" not in text.lower() and "发票" not in text:
            flags.append("no_key_fields")
    result["qc_flags"] = flags
    result["qc_pass"] = len(flags) == 0
    if flags:
        result["confidence"] = min(result.get("confidence", 0.9), 0.4)  # 降置信 → 人工队列
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python ocr_vision.py <pdf_or_image> [--max-pages N] [--vision]"
        )
        sys.exit(1)

    path = sys.argv[1]
    mp = None
    use_vision = "--vision" in sys.argv
    if "--max-pages" in sys.argv:
        mp = int(sys.argv[sys.argv.index("--max-pages") + 1])

    if use_vision:
        # 显式走 vision 兜底轨（OpenAI 兼容端点）
        schema = default_schema()
        if path.lower().endswith(".pdf"):
            results = extract_from_pdf(path, "auto", schema, max_pages=mp)
        else:
            results = [extract_from_image(path, "auto", schema)]
        print(json.dumps(results, ensure_ascii=False, indent=2))
        for i, r in enumerate(results):
            bad = low_confidence_fields(r)
            if bad:
                print(f"Page {i + 1} low-confidence fields (need review): {bad}", file=sys.stderr)
    else:
        # 默认：文本层 -> 百度 OCR
        result = extract_document(path, max_pages=mp)
        out = {
            "source": result.get("source"),
            "confidence": result.get("confidence"),
            "char_count": len(result.get("raw_text", "")),
            "error": result.get("error"),
            "text": result.get("raw_text", "")[:3000],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
