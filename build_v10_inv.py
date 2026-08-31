#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V10.0 库存数据构建器（路径 A：重新内嵌）

数据源：用友经理账号导出的「存量查询-默认方案」全量 CSV
输出  ：把 CSV 映射为 V10.0 的 12 字段契约，并就地替换 index.html 内嵌静态底表

设计红线（来自《后台真实数据对接说明》）：
  - 只换数据层，不动搜索/UI/统计口径
  - cat/series/model/mem/color/hw 属商品元数据，更新库存时不应变 -> 优先复用老快照(product_meta by code)
  - 可售库存严格用 a(可用量)，q(现存量) 绝不回退
  - 演示机按 wh/name 含「演示机/样机/体验机/哑机/陈列」识别（用友无 demo 字段、库存类型也无演示机值）
  - 门店名与 17 店白名单保持一致（已实测逐字一致）
"""
import re, json, csv, sys, os, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
CSV_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "现存量_20260831_默认方案.csv")

# 17 店正式统计白名单
WHITELIST = [
    "华为高新金鹰授权体验店","华为高新中大国际授权体验店","华为高新大茂城授权体验店","华为朝阳益田授权体验店",
    "华为浐灞优享授权体验店","华为大兴路龙湖授权体验店","华为李家村万达授权体验店","华为长安万科广场授权体验店",
    "华为长乐授权体验店","华为凯德广场授权体验店","华为金辉环球授权体验店","华为西安SKP至臻店",
    "华为智能生活馆西安荟聚","华为智能生活馆西安大悦城","华为智能生活馆西安大唐不夜城","华为智能生活馆西安高新万达","华为华阳城合作店",
]
WL = set(WHITELIST)

NONSALE_RE = re.compile(r"演示机|演示样机|样机|体验机|体验样机|陈列哑机|陈列样机|展示机|哑机")

# 从老快照提取商品元数据（按 code 建索引，product 级字段）
def load_product_meta(html):
    m = re.search(
        r"const inventoryData\s*=\s*Array\.isArray\(window\.__LJC_INVENTORY_DATA__\)\s*\?\s*window\.__LJC_INVENTORY_DATA__\s*:\s*(\[)",
        html, re.S)
    if not m:
        raise RuntimeError("找不到 inventoryData 定义")
    start = m.end() - 1  # 指向 '['
    depth = 0; i = start; n = len(html)
    while i < n:
        c = html[i]
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                break
        elif c == '"':
            # 跳字符串
            i += 1
            while i < n:
                if html[i] == '\\':
                    i += 2; continue
                if html[i] == '"':
                    break
                i += 1
        i += 1
    arr_text = html[start:i+1]
    arr = json.loads(arr_text)
    meta = {}
    for it in arr:
        code = it.get("code")
        if code is None:
            continue
        if code not in meta:
            meta[code] = {
                "cat": it.get("cat", ""),
                "series": it.get("series", ""),
                "model": it.get("model", "标准"),
                "mem": it.get("mem", ""),
                "color": it.get("color", ""),
                "hw": bool(it.get("hw", False)),
                "name": it.get("name", ""),
            }
    return arr, meta

# 兜底：从 name 推导内存（与前端 canonicalMemory 同源思路）
def derive_mem(name):
    s = (name or "").replace("＋", "+")
    m = re.search(r"(\d{1,2})\s*GB\s*\+\s*(\d{1,4})\s*(TB|T|GB)?", s, re.I)
    if m:
        return f"{int(m.group(1))}+" + ("1024" if re.match(r"t", m.group(3) or "GB", re.I) else (m.group(2) or "512" if m.group(2) else "512"))
    m = re.search(r"(?:^|[^0-9])(\d{1,2})\s*\+\s*(\d{1,4})\s*(TB|T|GB)?", s, re.I)
    if m:
        return f"{int(m.group(1))}+" + ("1024" if re.match(r"t", m.group(3) or "GB", re.I) else (m.group(2) or "512"))
    return ""

CORE_CATS = {"手机","平板电脑","穿戴","电脑","音频","智慧屏"}
# 兜底品类推导（仅在 code 不在老快照时启用）
def derive_cat(name, klass):
    s = (name or "")
    if re.search(r"平板|MatePad|Pad", s, re.I): return "平板电脑"
    if re.search(r"MateBook|笔记本|Book|MateStation|台式", s, re.I): return "电脑"
    if re.search(r"FreeBuds|耳机|FreeClip|FreeLace|Sound|音箱|耳机", s, re.I): return "音频"
    if re.search(r"智慧屏|Vision|电视", s, re.I): return "智慧屏"
    if re.search(r"Watch|手表|手环|穿戴|GT\d|Band", s, re.I): return "穿戴"
    if re.search(r"服务卡|膜|权益|贴膜|保护", s, re.I): return "增值服务"
    if re.search(r"手提袋|礼品|促销", s, re.I): return "促销品"
    if re.search(r"Mate|nova|Pura|畅享|优享|hi|畅玩|麦芒", s, re.I): return "手机"
    return (klass or "其他")

def to_int(v):
    try:
        return int(float((v or "0").replace(",", "")))
    except Exception:
        return 0

def main():
    html = open(INDEX, encoding="utf-8").read()
    old_arr, meta = load_product_meta(html)
    print(f"[meta] 老快照记录数={len(old_arr)}  商品元数据(code)数={len(meta)}")

    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8-sig")))
    print(f"[csv] 用友行数={len(rows)}")

    out = []
    reused = 0; new_code = 0; demo = 0; inwl = 0; zero_a = 0
    for r in rows:
        code = (r.get("物料SKU编码") or "").strip()
        name = (r.get("物料SKU名称") or "").strip()
        wh = (r.get("*仓库") or "").strip()
        if not code:
            continue
        q = to_int(r.get("*现存量"))
        a = to_int(r.get("*可用量"))  # 可售库存严格用可用量
        if a == 0:
            zero_a += 1
        # 演示机判定：wh/name/分类名称 含非售词（用友无 demo 字段）
        is_demo = bool(NONSALE_RE.search(wh) or NONSALE_RE.search(name) or NONSALE_RE.search(r.get("*分类名称") or ""))
        if is_demo:
            demo += 1
        # 商品元数据：优先复用老快照，保证 cat/series/model/mem/color/hw 与审计基线一致
        m = meta.get(code)
        if m:
            cat = m["cat"]; series = m["series"]; model = m["model"]; mem = m["mem"]; color = m["color"]; hw = m["hw"]
            reused += 1
        else:
            cat = derive_cat(name, r.get("*分类名称"))
            series = name; model = "标准"; mem = derive_mem(name); color = ""; hw = cat in CORE_CATS
            new_code += 1
        if wh in WL:
            inwl += 1
        out.append({
            "code": code, "name": name, "wh": wh, "cat": cat,
            "q": q, "a": a, "series": series, "model": model,
            "mem": mem, "color": color, "demo": is_demo, "hw": hw,
        })

    # 去重：同门店同 SKU 重复行聚合（说明第10条：重复行不能重复计数）
    agg = {}
    for it in out:
        k = (it["code"], it["wh"])
        if k in agg:
            agg[k]["q"] += it["q"]; agg[k]["a"] += it["a"]
        else:
            agg[k] = dict(it)
    out = list(agg.values())

    print(f"[map] 输出记录数={len(out)}  复用元数据={reused}  新code={new_code}  演示机={demo}  命中17店={inwl}  a=0={zero_a}")
    print(f"[map] 聚合后记录数={len(out)}")

    # 对账：与老快照按 (code,wh) 重叠比较元数据一致性
    old_by_k = {}
    for it in old_arr:
        old_by_k.setdefault((it.get("code"), it.get("wh")), it)
    mismatch = 0; overlap = 0
    for it in out:
        o = old_by_k.get((it["code"], it["wh"]))
        if not o:
            continue
        overlap += 1
        for f in ("cat", "series", "model", "mem", "color"):
            if (it.get(f) or "") != (o.get(f) or ""):
                mismatch += 1
                break
    if overlap:
        print(f"[recon] (code,wh)重叠={overlap}  元数据不一致记录={mismatch}  一致率={100*(overlap-mismatch)/overlap:.2f}%")

    # 写 JSON 产物（便于核对，不进 git 也可）
    with open(os.path.join(HERE, "inventory.gen.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    # 替换 index.html 内嵌静态底表
    new_arr_text = json.dumps(out, ensure_ascii=False)
    pat = re.compile(
        r"(const inventoryData\s*=\s*Array\.isArray\(window\.__LJC_INVENTORY_DATA__\)\s*\?\s*window\.__LJC_INVENTORY_DATA__\s*:\s*)(\[.*?\])(\s*;)",
        re.S)
    new_html, cnt = pat.subn(lambda mm: mm.group(1) + new_arr_text + mm.group(3), html, count=1)
    if cnt != 1:
        raise RuntimeError(f"替换失败，匹配数={cnt}")

    # ===== 新鲜度修复：每次刷新重写右上角时间与发布快照时间 =====
    # 之前只换 inventoryData 不换时间，导致用户无法判断是否最新版。
    # updateTime(页面右上角) 与 dataSnapshot(发布信息) 统一改为本次刷新时刻。
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    new_html, c1 = re.subn(
        r'(<b id="updateTime">)[^<]*(</b>)',
        lambda m: m.group(1) + now + m.group(2),
        new_html, count=1)
    new_html, c2 = re.subn(
        r"dataSnapshot:'[^']*'",
        f"dataSnapshot:'{now}'",
        new_html, count=1)
    print(f"[stamp] 更新时间 -> {now}  (updateTime替换={c1}, dataSnapshot替换={c2})")

    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(new_html)
    print(f"[done] index.html 已重建，新静态底表 {len(out)} 条")

if __name__ == "__main__":
    main()
