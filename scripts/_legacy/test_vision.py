#!/usr/bin/env python3
"""Smoke-test a vision model on a scanned import document.

Loads config from ../.env (parent of scripts/), tries OCR_VISION_MODEL,
and falls back to a secondary model on error so we know which works.
"""
import os
import sys
import json
import pathlib

# ---- load .env (sibling of this script's parent) -------------------------
ENV_PATH = pathlib.Path(__file__).resolve().parent.parent / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ocr_vision as ov  # noqa: E402

TEST_IMG = (
    r"C:\Users\34009\WorkBuddy\2026-07-29-10-01-16\work\render26B"
    r"\PRG-SUR-LECKER-26B-01证书检疫证.pdf_p1.png"
)

SCHEMA = {
    "document_type": "单据类型",
    "cert_no": "证书编号",
    "origin_country": "原产国(地区)",
    "departure_country": "输出国家或地区",
    "product": "品名",
    "production_factory": "生产厂家",
    "consignee": "收货人",
}


def try_model(model: str):
    print(f"\n===== TESTING MODEL: {model} =====")
    ov.MODEL = model
    try:
        res = ov.extract_from_image(TEST_IMG, "quarantine_cert", SCHEMA)
    except Exception as e:
        body = ""
        if hasattr(e, "response") and getattr(e, "response", None) is not None:
            try:
                body = e.response.text[:800]
            except Exception:
                pass
        print("ERROR:", type(e).__name__, str(e)[:300])
        if body:
            print("RESPONSE_BODY:", body)
        return False, None
    print(json.dumps(res, ensure_ascii=False, indent=2))
    # crude vision-success check: did it read the output-country field?
    fields = res.get("fields", {}) if isinstance(res, dict) else {}
    dep = fields.get("departure_country", {})
    val = dep.get("value") if isinstance(dep, dict) else None
    ok = bool(val) and str(val).strip()
    print(f">>> vision_ok (read departure_country='{val}'): {ok}")
    return ok, res


def main():
    primary = os.getenv("OCR_VISION_MODEL", "V4-FLASH")
    fallback = "V4-PRO" if primary.lower() != "v4-pro" else "V4-FLASH"
    ok, _ = try_model(primary)
    if not ok:
        print(f"\n[primary {primary} failed/weak] -> falling back to {fallback}")
        ok2, _ = try_model(fallback)
        print("\n===== RESULT =====")
        print(f"primary={primary} ok={ok}; fallback={fallback} ok={ok2}")
        print("RECOMMENDED_MODEL=" + (primary if ok else fallback))
    else:
        print("\n===== RESULT =====")
        print(f"primary={primary} ok={ok}")
        print("RECOMMENDED_MODEL=" + primary)


if __name__ == "__main__":
    main()
