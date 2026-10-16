# -*- coding: utf-8 -*-
import sys, io, pathlib, json, datetime, re
try:                                     # 幂等重设编码（不要新建 TextIOWrapper，否则旧 wrapper 被 GC 会关掉 buffer）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = pathlib.Path(__file__).resolve().parent.parent
TICKETS = ROOT / "runtime" / "tickets"
OUT_HTML = ROOT / "runtime" / "票总览.html"

# 供应商集团 -> 目录名 / 展示短名（与 pipeline_core.SUP_GROUP_CN 保持一致）
SUP_DIR_CN = {
    "SUP_PRG": "SUP_PRG", "SUP_AMERICAN_SEAFOODS": "SUP_美国海产", "SUP_ULKA": "SUP_ULKA",
    "SUP_LECKER_INDUSTRY": "SUP_日水", "SUP_THESEAFOODCOMPANY": "SUP_TheSeafoodCompany",
    "SUP_DALU": "SUP_DALU", "SUP_SONGTIAN": "SUP_松田", "SUP_CMCC": "SUP_CMCC", "SUP_GID": "SUP_GID",
}
SUP_SHORT = {
    "SUP_PRG": "PRG", "SUP_ULKA": "ULKA", "SUP_AMERICAN_SEAFOODS": "美国海产",
    "SUP_LECKER_INDUSTRY": "日水", "SUP_DALU": "DALU", "SUP_SONGTIAN": "松田",
    "SUP_THESEAFOODCOMPANY": "TheSeafoodCompany", "SUP_CMCC": "CMCC", "SUP_GID": "GID",
}
MAT_CN = {"pollock": "鳕鱼", "itoroyi": "金线", "fillet": "鱼片", "other": "其他"}


def load_archive_root():
    """红线区根：优先读 config/paths.yaml（唯一路径出口），失败回退默认。"""
    cfg = ROOT / "config" / "paths.yaml"
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            m = re.match(r'\s*archive_root:\s*"([^"]+)"', line)
            if m:
                return pathlib.Path(m.group(1))
    return pathlib.Path(r"C:/Users/34009/Desktop/03_合同与订单/进口单证归档")


def build_real_l4_index(known_tids):
    """扫红线区得到 {票ID: 真实L4目录名}，修「打开整理目录」指向错。

    2026-10-16 起改用「已知票ID 是否出现在目录名里」匹配，**同时兼容**
    L4 加发票号前后两种命名（旧：`202607-17.58t-PRG-...-26A-07`；
    新：`202607-17.58t-PRG-...-26A-07-PRGINV2607-041`）。只读。
    """
    idx = {}
    root = load_archive_root()
    if not root.exists():
        return idx
    for p in root.rglob("*"):
        if p.is_dir() and p.parent.name in ("鳕鱼", "金线", "鱼片", "其他"):
            for tid in known_tids:
                if tid in p.name:
                    idx[tid] = p.name
                    break
    return idx

def load():
    out = []
    for idx in sorted(TICKETS.glob("*_index.json")):
        out.append(json.loads(idx.read_text(encoding="utf-8")))
    return out

def dedup(card):
    docs = card["docs"]; seen = set(); res = []
    for d in docs:
        h = d.get("ocr","")
        if h and h in seen: continue
        if h: seen.add(h)
        res.append(d)
    return res

def main():
    tickets = load()
    real_l4 = build_real_l4_index([c["ticket_id"] for c in tickets])   # 兼容加发票号前后的 L4 命名
    data = []
    for c in tickets:
        docs = dedup(c)
        compl = c.get("completeness",{})
        grp = c.get("supplier_group","")
        data.append({
            "label": c.get("friendly_label", c["ticket_id"]),
            "tid": c["ticket_id"],
            "grp": grp,
            "supplier": SUP_SHORT.get(grp, grp),
            "material": c.get("material",""),
            "invoice_no": c.get("invoice_no","") or "",
            "bl": c.get("bl_numbers",[]),
            "date": c.get("date",""),
            "year": (c.get("date","") or "")[:4],
            "missing": compl.get("missing",[]),
            "cond": compl.get("conditional_missing",[]),
            "alerts": [a["msg"] for a in c.get("alerts",[])],
            "n": len(docs),
            "types": {},
            "inv": c.get("invoice_detail",{}),
            "folder": c.get("folder",""),
            "folder_html": "",
            "inv_display": "",
            "prod_display": "",
        })
        for d in docs:
            data[-1]["types"][d["type"]] = data[-1]["types"].get(d["type"],0)+1
    # 补整理目录路径（快速定位）——优先用红线区真实目录名，避免 L4 年月口径不一致导致指向错
    for t in data:
        sup_dir = SUP_DIR_CN.get(t["grp"], t["grp"])
        mat = MAT_CN.get(t["material"], t["material"])
        # 兜底：按友好标签推 L4（{年月}-{吨位}t-{票ID}）
        m_l = re.search(r"-(\d{6})-(\d+(?:\.\d+)?)t$", t["label"])
        if m_l:
            ym, ton = m_l.group(1), m_l.group(2)
            l4_fallback = f"{ym}-{ton}t-{t['tid']}"
        else:
            m_t = re.search(r"-(\d+(?:\.\d+)?)t$", t["label"])
            ton = m_t.group(1) if m_t else ""
            l4_fallback = f"{t['date'][:6]}-{ton}t-{t['tid']}" if ton else t["tid"]
        l4 = real_l4.get(t["tid"], l4_fallback)          # 真实目录优先
        t["folder"] = f"{sup_dir}\\{mat}\\{l4}"
        t["folder_verified"] = t["tid"] in real_l4       # False = 红线区未找到，路径为推算值
        # 发票明细展示：支持多等级，每组 等级/数量/价格，用空位分隔（尽量一行）
        inv = t["inv"]
        grades = inv.get("grades", [])
        if grades:
            groups = ["/".join([g[0], g[1], g[2]]) for g in grades if len(g)>=3]
            t["inv_display"] = "&nbsp;&nbsp;&nbsp;".join(groups)  # 用HTML空格分隔
        else:
            parts = [inv.get("grade",""), inv.get("qty",""), inv.get("price","")]
            t["inv_display"] = "/".join([p for p in parts if p])
        # 生产日期 + 捕捞区域（同一行）
        pd_parts = []
        if inv.get("prod_date"):
            pd_parts.append(f"生产日期: {inv['prod_date']}")
        if inv.get("fao"):
            pd_parts.append(f"捕捞区域: {inv['fao']}")
        t["prod_display"] = "　".join(pd_parts)
        # 定位文件夹 HTML（Python 端拼好，避免 JS 转义问题）
        if t["folder"]:
            mark = "" if t.get("folder_verified") else "（红线区未找到，此为推算路径）"
            t["folder_html"] = ('<a href="#" onclick="openFolder(&quot;' + t["folder"] +
                                '&quot;);return false;" style="color:#2563eb;text-decoration:underline;font-size:12px">📂 打开整理目录</a>' +
                                (f'<span style="color:#b45309;font-size:12px">{mark}</span>' if mark else ""))
    # 生成 HTML
    # json.dumps 输出即为合法 JS 字面量（换行转义为 \n）；不要二次 replace，
    # 否则路径里的单反斜杠会被翻倍、真实换行会变成字面 \n（历史 BUG）。
    data_json = json.dumps(data, ensure_ascii=False)
    gen = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>进口单证票总览</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Microsoft YaHei",sans-serif;background:#f2f4f8;color:#222;padding:16px}}
h1{{font-size:20px;margin-bottom:4px}}
.gen{{color:#888;font-size:12px;margin-bottom:10px}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}}
.stat{{background:#fff;border-radius:8px;padding:8px 16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.stat b{{font-size:20px;display:block}}
.filters{{position:sticky;top:0;background:#f2f4f8;padding:8px 0;z-index:9;display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}}
.layerbar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.lbtn{{border:2px solid #2563eb;background:#fff;color:#2563eb;border-radius:8px;padding:6px 14px;font-size:13px;font-weight:bold;cursor:pointer}}
.lbtn.on{{background:#2563eb;color:#fff}}
.tab{{border:1px solid #ccc;background:#fff;border-radius:16px;padding:4px 14px;cursor:pointer;font-size:13px}}
.tab.on{{background:#2563eb;color:#fff;border-color:#2563eb}}
input,select{{padding:6px 10px;border:1px solid #ccc;border-radius:6px;font-size:13px}}
.ticket{{background:#fff;border-radius:10px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08);overflow:hidden}}
.thead{{background:#eaf0fb;padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
.thead .label{{font-size:16px;font-weight:bold}}
.badge{{font-size:12px;border-radius:10px;padding:1px 10px;color:#fff}}
.b.缺{{background:#ef4444}}.b.齐{{background:#22c55e}}.b.条件{{background:#f59e0b}}.b.报警{{background:#dc2626}}
.body{{padding:12px 16px}}
.row{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:6px}}
.chip{{font-size:12px;background:#f8fafc;border:1px solid #ddd;border-radius:6px;padding:3px 10px}}
.chip b{{color:#2563eb}}
.missing{{background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:8px 12px;margin:8px 0;font-size:13px;color:#b91c1c}}
.alerts{{background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:8px 12px;margin:8px 0;font-size:13px;color:#92400e}}
.empty{{text-align:center;color:#888;padding:40px;font-size:14px}}
</style></head><body>
<h1>进口单证票总览</h1>
<div class="gen">生成时间：{gen} ｜ 数据源：runtime/tickets/*_index.json（唯一真源）｜ 双击 bat 刷新</div>

<!-- 筛选栏：分层 + 状态 + 供应商 + 物料 + 搜索 -->
<div class="filters">
  <div class="layerbar" id="layerbar">
    <button class="lbtn" data-layer="all" onclick="setLayer('all')">全部</button>
    <button class="lbtn" data-layer="full" onclick="setLayer('full')">全齐</button>
    <button class="lbtn" data-layer="cond" onclick="setLayer('cond')">条件缺</button>
    <button class="lbtn" data-layer="miss" onclick="setLayer('miss')">缺必备</button>
    <button class="lbtn" data-layer="alert" onclick="setLayer('alert')">有报警</button>
  </div>
  <select id="supSel" onchange="setSup(this.value)">
    <option value="">供应商:全部</option>
    <option value="PRG">PRG</option>
    <option value="ULKA">ULKA</option>
  </select>
  <select id="matSel" onchange="setMat(this.value)">
    <option value="">物料:全部</option>
    <option value="pollock">鳕鱼</option>
    <option value="itoroyi">金线</option>
    <option value="fillet">鱼片</option>
  </select>
  <select id="yearSel" onchange="setYear(this.value)">
    <option value="">年份:全部</option>
    <option value="2025">2025</option>
    <option value="2026">2026</option>
  </select>
  <input id="kw" placeholder="搜索 票ID/发票号/提单/单证类型..." oninput="setKw(this.value)">
</div>

<div class="stats" id="stats"></div>
<div id="content"></div>
<script>
const DATA = {data_json};
// 全局筛选状态
let LAYER = 'all', SUP = '', MAT = '', KW = '', YEAR = '';

// 判断票是否匹配当前筛选
function matchTicket(t){{
  // 资料分层（互斥）：全齐 / 条件缺 / 缺必备 / 有报警
  const isFull = t.missing.length===0 && t.cond.length===0;
  const isCond = t.missing.length===0 && t.cond.length>0;
  const isMiss = t.missing.length>0;
  const isAlert = t.alerts.length>0;
  if (LAYER==='full' && !isFull) return false;        // 全齐：必须全齐
  if (LAYER==='cond' && !isCond) return false;        // 条件缺：必须条件缺
  if (LAYER==='miss' && !isMiss) return false;        // 缺必备：必须缺必备
  if (LAYER==='alert' && !isAlert) return false;      // 有报警：必须有报警
  if (SUP && t.supplier!==SUP) return false;
  if (MAT && t.material!==MAT) return false;
  if (YEAR && t.year!==YEAR) return false;
  if (KW){{
    const hay = (t.label+' '+t.tid+' '+t.bl.join(' ')+' '+(t.invoice_no||'')+' '+Object.keys(t.types).join(' ')).toLowerCase();
    if (!hay.includes(KW.toLowerCase())) return false;
  }}
  return true;
}}

// 更新筛选按钮高亮
function refreshBar(){{
  document.querySelectorAll('.lbtn').forEach(b=>b.classList.toggle('on', b.dataset.layer===LAYER));
}}

function setLayer(v){{LAYER=v;refreshBar();render();}}
function setSup(v){{SUP=v;render();}}
function setMat(v){{MAT=v;render();}}
function setYear(v){{YEAR=v;render();}}
function setKw(v){{KW=v;render();}}

function render(){{
  const list = DATA.filter(matchTicket);
  const nTodo = DATA.filter(t=>t.missing.length>0||t.cond.length>0).length;
  const nAlert = DATA.filter(t=>t.alerts.length>0).length;
  document.getElementById("stats").innerHTML =
    `<div class="stat"><b>${{DATA.length}}</b>总票数</div>` +
    `<div class="stat"><b>${{list.length}}</b>当前显示</div>` +
    `<div class="stat"><b>${{nTodo}}</b>票待办</div>` +
    `<div class="stat"><b>${{nAlert}}</b>票有报警</div>`;
  if (list.length===0){{
    document.getElementById("content").innerHTML = '<div class="empty">没有匹配的票（试试放宽筛选/清空搜索）</div>';
    return;
  }}
  let html = "";
  for(const t of list){{
    const miss = t.missing.length>0 ? `<div class="missing">缺必备：${{t.missing.join("、")}}</div>` : "";
    const cond = t.cond.length>0 ? `<div class="missing" style="background:#fffbeb;border-color:#fde68a;color:#92400e">条件缺：${{t.cond.join("、")}}</div>` : "";
    const alerts = t.alerts.length>0 ? `<div class="alerts">⚠️ ${{t.alerts.join("；")}}</div>` : "";
    const bl = t.bl.length>0 ? t.bl.join(",") : "无";
    const types = Object.entries(t.types).map(function(ent){{var k=ent[0],v=ent[1];return '<span class="chip"><b>'+k+'</b> ×'+v+'</span>';}}).join("");
    const st = t.missing.length>0 ? "缺" : (t.cond.length>0 ? "条件" : "齐");
    const st_txt = t.missing.length>0 ? "缺必备" : (t.cond.length>0 ? "条件缺" : "齐全");
    // 发票明细 + 生产日期 + 定位（Python端已拼好）
    const inv_txt = t.inv_display || "";
    const prod = t.prod_display || "";
    const folder = t.folder_html || "";
    html += `<div class="ticket">
      <div class="thead">
        <span class="label">${{t.label}}</span>
        <span>${{t.supplier}} / ${{t.material}} / ${{t.date}} / 发票号:${{t.invoice_no||'-'}} / 提单:${{bl}}</span>
        <span class="badge b ${{st}}">${{st_txt}}</span>
        <span>${{t.n}}份单据</span>
      </div>
      <div class="body">
        ${{miss}}${{cond}}${{alerts}}
        ${{inv_txt ? '<div class="row"><span class="chip" style="background:#e0f2fe;border-color:#38bdf8"><b>发票明细</b> ' + inv_txt + '</span></div>' : ""}}
        ${{prod ? '<div class="row"><span class="chip" style="background:#f0fdf4;border-color:#86efac">' + prod + '</span></div>' : ""}}
        <div class="row">${{types}}</div>
        ${{folder ? '<div class="row">' + folder + '</div>' : ""}}
      </div>
    </div>`;
  }}
  document.getElementById("content").innerHTML = html;
}}
function openFolder(folder){{
  // 打开 F 盘整理目录对应文件夹
  const base = 'F:/采购资料/采购部资料/采购部供方管理,订单，合同 尾牙/原料供应商/鳕鱼相关/进口原料单证整理/';
  const path = base + folder;
  // 用 file:// 协议或 cmd start 打开（浏览器限制，用转义路径）
  try {{
    const full = path.split('/').join('/');
    // 提示用户（浏览器不能直接开文件夹，但可复制路径）
    alert('整理目录：' + full);
    navigator.clipboard.writeText(full).catch(()=>{{}});
  }} catch(e) {{ }}
}}
refreshBar();
render();
</script>
</body></html>"""
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"票总览已生成: {OUT_HTML} | 共 {len(data)} 票")

if __name__ == "__main__":
    main()