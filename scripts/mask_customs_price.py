# -*- coding: utf-8 -*-
"""报关单去价：生成"不带价"版（删 单价/总价/币制 三格，含美元）供质量/第三方审核。
用法: python scripts/mask_customs_price.py <带价报关单.pdf> [输出.pdf]
技术: rotation 90/0 通用；定位"价格数值(小数)/美元" span →
      取其 x/y 外包（动态范围，兼容单商品/多商品多行）→ redact 清除。
只新增副本，不改源文件（红线铁律）。
"""
import sys, re, pathlib, tempfile, os
import fitz

def _is_money(t):
    # 2026-10-15 修：原 `\d{1,4}\.\d{2,4}` 漏掉 ≥5 位整数的总价（如 149877.00）→ 会残留价格。
    # 放宽为「1+位数字（可含千分位）+ 2~4 位小数」，仍不匹配纯整数（箱数/件数），避免误删数量。
    return t in ('美元', 'USD', 'CNY', 'RMB', '人民币') or bool(re.fullmatch(r'\d[\d,]*\.\d{2,4}', t))

def _money_spans(pg):
    rs = []
    for b in pg.get_text('dict')['blocks']:
        if b.get('type') != 0:
            continue
        for l in b['lines']:
            for sp in l['spans']:
                if _is_money(sp['text'].strip()):
                    rs.append(fitz.Rect(sp['bbox']))
    return rs

def make_noprice(src, dst=None):
    src = pathlib.Path(src)
    dst = pathlib.Path(dst) if dst is not None else src.with_name(src.stem + '_不带价.pdf')
    d = fitz.open(str(src))
    n = 0
    for pi in range(len(d)):
        pg = d[pi]
        rs = _money_spans(pg)
        if not rs:
            continue
        x0 = min(r.x0 for r in rs) - 3
        x1 = max(r.x1 for r in rs) + 3
        y0 = min(r.y0 for r in rs) - 3
        y1 = max(r.y1 for r in rs) + 3
        pg.add_redact_annot(fitz.Rect(x0, y0, x1, y1), fill=(1, 1, 1))
        pg.apply_redactions()
        n += 1
    fd, tmp = tempfile.mkstemp(suffix='.pdf', dir=str(dst.parent)); os.close(fd)
    d.save(tmp); d.close()
    os.replace(tmp, str(dst))
    return dst, n


def verify_no_price(pdf_path):
    """落盘后自检：重新打开输出件，统计残留的金额样式 span（0 = 干净）。"""
    left = []
    with fitz.open(str(pdf_path)) as d:
        for pi in range(len(d)):
            for b in d[pi].get_text('dict')['blocks']:
                if b.get('type') != 0:
                    continue
                for l in b['lines']:
                    for sp in l['spans']:
                        if _is_money(sp['text'].strip()):
                            left.append((pi + 1, sp['text'].strip()))
    return left


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    out, n = make_noprice(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(f'已生成不带价版: {out} ({n} 页含价格已清)')
    left = verify_no_price(out)
    if left:
        print(f'⚠ 自检：仍残留 {len(left)} 处金额样式（需人工确认/调边界）:')
        for pg, t in left[:20]:
            print(f'    p{pg}: {t}')
        sys.exit(2)
    print('自检通过：未发现残留金额')
