#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全比对：直连接口新 csv vs 当前 index.html 内嵌数据（不修改任何文件）"""
import json, csv, re, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
CSV = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "现存量_20260903_默认方案.csv")

WHITELIST = [
    "华为高新金鹰授权体验店","华为高新中大国际授权体验店","华为高新大茂城授权体验店","华为朝阳益田授权体验店",
    "华为浐灞优享授权体验店","华为大兴路龙湖授权体验店","华为李家村万达授权体验店","华为长安万科广场授权体验店",
    "华为长乐授权体验店","华为凯德广场授权体验店","华为金辉环球授权体验店","华为西安SKP至臻店",
    "华为智能生活馆西安荟聚","华为智能生活馆西安大悦城","华为智能生活馆西安大唐不夜城","华为智能生活馆西安高新万达","华为华阳城合作店",
]
WL = set(WHITELIST)

# 解析 csv
csv_rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
csv_by = {}
for r in csv_rows:
    code = (r.get("物料SKU编码") or "").strip()
    wh = (r.get("*仓库") or "").strip()
    if not code:
        continue
    try: q = int(float(r.get("*现存量") or 0))
    except: q = 0
    try: a = int(float(r.get("*可用量") or 0))
    except: a = 0
    csv_by[(code, wh)] = {"q": q, "a": a, "cat": (r.get("*分类名称") or "").strip()}

# 解析 index.html 内嵌数组
html = open(INDEX, encoding="utf-8").read()
MARK = re.compile(r"const inventoryDataRaw\s*=\s*Array\.isArray\(window\.__LJC_INVENTORY_DATA__\)\s*\?\s*window\.__LJC_INVENTORY_DATA__\s*:\s*\[")
m = MARK.search(html)
start = m.end() - 1
depth = 0; i = start; n = len(html)
while i < n:
    c = html[i]
    if c == '[': depth += 1
    elif c == ']':
        depth -= 1
        if depth == 0: break
    elif c == '"':
        i += 1
        while i < n:
            if html[i] == '\\': i += 2; continue
            if html[i] == '"': break
            i += 1
    i += 1
html_arr = json.loads(html[start:i+1])
html_by = {(it.get("code"), it.get("wh")): it for it in html_arr}

def split(d):
    wl = {k: v for k, v in d.items() if k[1] in WL}
    other = {k: v for k, v in d.items() if k[1] not in WL}
    return wl, other

csv_wl, csv_other = split(csv_by)
html_wl, html_other = split(html_by)

print("=" * 50)
print("【总量】")
print("  csv 总行数(有code): %d" % len(csv_by))
print("  html 内嵌记录数:    %d" % len(html_by))
print("  csv 命中17店: %d    html 命中17店: %d" % (len(csv_wl), len(html_wl)))
print("  csv 非17店:    %d    html 非17店:    %d" % (len(csv_other), len(html_other)))

print("=" * 50)
print("【17 店逐条比对 (code,wh)】")
only_csv = set(csv_wl) - set(html_wl)
only_html = set(html_wl) - set(csv_wl)
both = set(csv_wl) & set(html_wl)
print("  仅在 csv(API): %d" % len(only_csv))
print("  仅在 html(旧): %d" % len(only_html))
print("  两边都有:       %d" % len(both))

q_mis = a_mis = 0
for k in both:
    c = csv_wl[k]; h = html_wl[k]
    if c["q"] != (h.get("q") or 0): q_mis += 1
    if c["a"] != (h.get("a") or 0): a_mis += 1
print("  q 不一致: %d    a 不一致: %d" % (q_mis, a_mis))

if only_csv:
    print("  --- 仅 API 有(前10):", only_csv[:10])
if only_html:
    print("  --- 仅 旧有(前10):", only_html[:10])

# 17 店 q/a 总量对比
def tot(d, f): return sum(v.get(f, 0) for v in d.values())
print("=" * 50)
print("【17 店 q/a 合计】")
print("  q:  API=%d   旧=%d" % (tot(csv_wl, "q"), tot(html_wl, "q")))
print("  a:  API=%d   旧=%d" % (tot(csv_wl, "a"), tot(html_wl, "a")))
print("=" * 50)
