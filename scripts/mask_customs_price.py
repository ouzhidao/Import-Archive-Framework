# -*- coding: utf-8 -*-
"""报关单去价：生成"不带价"版（删 单价/总价/币制 三格，含美元）供质量/第三方审核。
用法:
  python scripts/mask_customs_price.py <带价报关单.pdf> [输出.pdf]
若不指定输出，自动在同目录生成 {原名去后缀}_不带价.pdf
技术: rotation=90 竖排版式，价格数据格在逻辑坐标 x∈[283,326]、y≈344(可多行)；
     定位"小数/美元" span → 取 y 并集 → redact 清除。
注: 只新增副本，不改源文件（红线铁律 copy 不移动）。
"""
import sys, re, pathlib
import fitz

X_LO, X_HI = 283.0, 326.0   # 单价/总价/币制 数据列 x 带
PAD_Y = 3.0                  # y 带上下留白

def _price_spans(pg):
    """返回价格 span 的 (y0,y1) 列表（含单价小数/总价小数/美元币制）"""
    ys = []
    td = pg.get_text('dict')
    for b in td['blocks']:
        if b.get('type') != 0:
            continue
        for l in b['lines']:
            for sp in l['spans']:
                t = sp['text'].strip()
                bb = sp['bbox']
                is_money_num = re.fullmatch(r'\d{1,4}\.\d{2,4}', t)
                is_usd = t in ('美元', 'USD', '币制')
                if X_LO <= bb[0] <= X_HI and (is_money_num or is_usd):
                    ys.append((bb[1], bb[3]))
    return ys

def make_noprice(src, dst=None):
    src = pathlib.Path(src)
    if dst is None:
        dst = src.with_name(src.stem + '_不带价.pdf')
    d = fitz.open(str(src))
    n_masked = 0
    for pi in range(len(d)):
        pg = d[pi]
        ys = _price_spans(pg)
        if not ys:
            continue
        y0 = min(a for a, _ in ys) - PAD_Y
        y1 = max(b for _, b in ys) + PAD_Y
        pg.add_redact_annot(fitz.Rect(X_LO, y0, X_HI, y1), fill=(1, 1, 1))
        pg.apply_redactions()
        n_masked += 1
    d.save(str(dst))
    d.close()
    return dst, n_masked

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    out, n = make_noprice(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(f'已生成不带价版: {out} ({n} 页含价格已清)')
