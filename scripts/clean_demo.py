# -*- coding: utf-8 -*-
"""
进口单证归档 —— 单票清洗示范脚本（仅处理一票，演示用）
作用：备份原 zip -> 解压指定票到分层目录 -> 按规则重命名+分类 -> 对"莱卡尔HASL"类打开PDF确认类型
数据安全：原 zip 不动；只新建目录与副本；不写真实税号/账号字面量；不推送。
"""
import zipfile, os, shutil, pdfplumber

SRC = r'E:/软件/xwechat_files/oop88pop_3f3a/temp/RWTemp/2026-07/ad8561460aaf9990ae1998d4c3a170d7/日本水产LECKER INDUSTRY CO.,LTD(1).zip'
ROOT = r'C:/Users/34009/Desktop/03_合同与订单/进口单证归档'
SUP, ORIG, YEAR = 'SUP_日本LECKER_INDUSTRY', 'ORIG_日本', '2024'
TICKET = '20240329_鱼糜96t_LS-24-01-01'
PREFIX = '20240329 96吨鱼糜/No.LS-24-01-01 48吨鱼糜/'
DATE, CONTRACT = '20240329', 'LS-24-01-01'

SUBS = ['01_合同及附加', '02_发票箱单', '03_提单',
        '04_官方通关单证', '05_质量与付款', '06_标签及其他']

def status(fn):
    if '未加密' in fn: return '未加密'
    if '副本' in fn: return '副本'
    if '盖章' in fn or '押印' in fn: return '盖章件'
    if 'cam' in fn.lower() or '扫描' in fn: return '扫描件'
    return '正本'

def subtype(fn, text=''):
    low = fn.lower()
    if any(k in fn for k in ['合同', '契约', '販売契約']) or 'sales contract' in low or 'contract' in low:
        return ('合同', '01_合同及附加')
    if '箱单' in fn or 'packing' in low:
        return ('箱单', '02_发票箱单')
    if '提单' in fn or 'b/l' in low or 'sbl' in low:
        return ('提单', '03_提单')
    if '发票' in fn or 'invoice' in low or ' iv ' in low or 'iv（' in low or '請求書' in fn or '領収書' in fn:
        return ('发票', '02_发票箱单')
    if '产地' in fn or 'certificate of origin' in low or 'form ' in low:
        return ('产地证', '04_官方通关单证')
    if '捕捞' in fn or 'catch' in low:
        return ('捕捞证', '04_官方通关单证')
    if '卫生' in fn or 'health' in low or '健康' in fn:
        return ('卫生证', '04_官方通关单证')
    if '检疫' in fn or 'quarantine' in low:
        return ('检疫证明', '04_官方通关单证')
    if '报关' in fn or 'customs' in low:
        return ('报关单', '04_官方通关单证')
    if '水单' in fn:
        return ('水单', '05_质量与付款')
    if '税单' in fn or '税费' in fn or '税' in fn:
        return ('税单税费', '04_官方通关单证')
    if 'analysis' in low or 'report' in low or '分析' in fn or '批次' in fn:
        return ('COA出厂报告', '05_质量与付款')
    if '标签' in fn or 'label' in low or 'ラベル' in fn or '申报要素' in fn:
        return ('标签', '06_标签及其他')
    if 'msds' in low or '声明' in fn:
        return ('符合性声明', '06_标签及其他')
    if '莱卡尔' in fn or 'hasl' in low:  # 需打开 PDF 确认
        t = text
        if '报关' in t or 'customs' in t.lower() or '申报' in t:
            return ('报关单', '04_官方通关单证')
        if '检疫' in t or 'quarantine' in t.lower():
            return ('检疫证明', '04_官方通关单证')
        if '税' in t or 'tax' in t.lower():
            return ('税单税费', '04_官方通关单证')
        return ('通关单证(待确认)', '04_官方通关单证')
    return ('其他', '06_标签及其他')

def main():
    base = os.path.join(ROOT, SUP, ORIG, YEAR, TICKET)
    if os.path.exists(base):
        shutil.rmtree(base)
    os.makedirs(base, exist_ok=True)
    for s in SUBS:
        os.makedirs(os.path.join(base, s), exist_ok=True)

    bak = os.path.join(ROOT, '_backup')
    os.makedirs(bak, exist_ok=True)
    shutil.copy(SRC, os.path.join(bak, os.path.basename(SRC)))
    print('[备份]', os.path.join(bak, os.path.basename(SRC)))

    z = zipfile.ZipFile(SRC)
    flat = []
    for n in z.namelist():
        if n.startswith(PREFIX) and not n.endswith('/'):
            fn = n[len(PREFIX):]
            if not fn:
                continue
            with open(os.path.join(base, fn), 'wb') as f:
                f.write(z.read(n))
            flat.append(fn)
    print(f'[提取] {len(flat)} 个文件到临时平铺区')

    moves = []
    for fn in flat:
        ext = '.' + fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
        st = status(fn)
        text = ''
        if fn.lower().endswith('.pdf') and ('莱卡尔' in fn or 'hasl' in fn.lower()):
            try:
                with pdfplumber.open(os.path.join(base, fn)) as pdf:
                    text = (pdf.pages[0].extract_text() or '')[:600]
            except Exception:
                text = ''
        typ, sub = subtype(fn, text)
        newname = f'{DATE}_{CONTRACT}_{typ}_{st}{ext}'
        shutil.move(os.path.join(base, fn), os.path.join(base, sub, newname))
        tag = 'OPEN' if ('莱卡尔' in fn or 'hasl' in fn.lower()) else 'RULE'
        moves.append((fn, f'{sub}/{newname}', tag))

    print('\n===== 映射结果 (原 -> 新) =====')
    for o, n, how in moves:
        print(f'  [{how}] {o}\n        -> {n}')

    print('\n===== 最终目录树 =====')
    for root, dirs, files in os.walk(base):
        if root == base:
            print(f'{TICKET}/')
            continue
        lvl = root.replace(base, '').count(os.sep)
        print(f"{'  ' * lvl}{os.path.basename(root)}/")
        for f in sorted(files):
            print(f"{'  ' * lvl}  {f}")

if __name__ == '__main__':
    main()
