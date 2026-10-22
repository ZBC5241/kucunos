#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_restock.py —— 生成「今日该备的货」清单并注入 index.html。

算法（李家村本店口径）：
  窗口      = 销售数据实际最新日期往前 WINDOW_DAYS(14) 天（不用今天，数据可能滞后一天）
  日均销量  = 窗口内该 SKU 销量合计 / 14
  本店可售  = index.html 内嵌库存中 wh=李家村 且非演示机行的可用量 a 合计（无行则 0）
  建议补    = ceil(日均 × LEAD_DAYS(7) − 本店可售)，下限 0

  入榜分两类：
   A 缺口类：本店有库存档案，建议补 ≥1                          → 角标「缺口 X」
   B 断货类：本店无库存档案，但全系统有档案、非服务/演示机品类、
             且近14天销量 ≥ NOSTOCK_MIN_SOLD(3)（滤掉一锤子串货） → 角标「本地无货」
             建议补 = ceil(日均 × 7)（7 天需求量）
  排序：建议补↓ 日均↓；取前 TOP_N 条。

数据来源：
  销量 = data/sales-cache.json（pull_sales.py 产出；超过 MAX_USABLE_AGE(7天) 视为不可用，
         注入空清单 + stale 标记，前端提示「数据暂不可用」，绝不拿旧数据冒充）
  库存 = index.html 内嵌 inventoryDataRaw 数组（当前线上同源）

注入锚点（与前端约定）：
  const restockPlanData = {...};
  首次运行若找不到锚点，会自动在 inventoryData 定义行之后插入（幂等）。
"""
import re, json, sys, os, math, datetime, time

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")
SALES_CACHE = os.path.join(HERE, "data", "sales-cache.json")
HOME = "华为李家村万达授权体验店"

WINDOW_DAYS = 14
LEAD_DAYS = 7
TOP_N = 12
NOSTOCK_MIN_SOLD = 3                 # 断货类入榜的最低 14 天销量（滤串货噪音）
NOSTOCK_EXCLUDE_CATS = {"增值服务", "演示机"}  # 服务/演示机不进货，不进断货类
MAX_USABLE_AGE = 7 * 86400           # 销售缓存超过 7 天不用（旧数据会误导备货决策）

# 与 build_v11_inv.py 相同的 V11.4 数据载体标记
INV_MARKER_RE = re.compile(
    r"const inventoryDataRaw\s*=\s*Array\.isArray\(window\.__LJC_INVENTORY_DATA__\)\s*\?\s*window\.__LJC_INVENTORY_DATA__\s*:\s*\["
)
RESTOCK_RE = re.compile(r"const restockPlanData = \{.*?\};", re.S)
INSERT_AFTER_RE = re.compile(r"(const inventoryData\s*=\s*inventoryDataRaw\.map\(__ljcNormalizeRow\);\s*\n)")


def find_bracket_end(html, start):
    """从 html[start]=='[' 开始做括号平衡匹配，返回配对的 ']' 下标。"""
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
            i += 1
            while i < n:
                if html[i] == '\\':
                    i += 2; continue
                if html[i] == '"':
                    break
                i += 1
        i += 1
    return i


def load_inventory(html):
    """解析 index.html 内嵌库存数组。
    返回 (home_own, sys_info)：
      home_own: {code: 本店可用量}（仅李家村非演示机行）
      sys_info: {code: {name, cat}}（全系统任一非演示机行，供断货类取名）
    """
    m = INV_MARKER_RE.search(html)
    if not m:
        raise RuntimeError("找不到 inventoryDataRaw 定义（MARKER 不匹配）")
    start = m.end() - 1
    end = find_bracket_end(html, start)
    arr = json.loads(html[start:end + 1])
    home_own, sys_info, name_stat = {}, {}, {}
    for r in arr:
        if r.get("demo"):
            continue
        code = (r.get("code") or "").strip()
        if not code:
            continue
        if code not in sys_info:
            sys_info[code] = {"name": r.get("name") or "", "cat": r.get("cat") or "其他"}
        if (r.get("wh") or "") == HOME:
            try:
                a = max(0, int(float(r.get("a")))) if r.get("a") is not None else 0
            except Exception:
                a = 0
            home_own[code] = home_own.get(code, 0) + a
            if r.get("name"):
                name_stat.setdefault(code, {})
                name_stat[code][r["name"]] = name_stat[code].get(r["name"], 0) + 1
    for code, st in name_stat.items():
        sys_info[code]["name"] = sorted(st.items(), key=lambda x: (-x[1], -len(x[0])))[0][0]
    return home_own, sys_info


def load_sales():
    """读销售缓存。返回 (ok, stale_reason, fetched_at, records)"""
    if not os.path.exists(SALES_CACHE):
        return False, "暂无销量数据缓存", 0, []
    try:
        d = json.load(open(SALES_CACHE))
    except Exception as e:
        return False, "销量缓存损坏: %s" % e, 0, []
    ts = d.get("fetched_at") or 0
    age = time.time() - ts
    if age > MAX_USABLE_AGE:
        return False, "销量数据已 %d 天未更新" % int(age / 86400), ts, []
    recs = d.get("records") or []
    if not recs:
        return False, "销量缓存无明细", ts, []
    return True, "", ts, recs


def build_items(home_own, sys_info, recs):
    """按窗口聚合销量，算补货缺口。"""
    dates = sorted({r.get("d") or "" for r in recs if r.get("d")})
    if not dates:
        return None, [], {}
    end = datetime.date.fromisoformat(dates[-1])
    start = end - datetime.timedelta(days=WINDOW_DAYS - 1)
    sold = {}
    for r in recs:
        d = r.get("d") or ""
        if not (start.isoformat() <= d <= end.isoformat()):
            continue
        sku = (r.get("sku") or "").strip()
        if not sku:
            continue
        try:
            qty = float(r.get("qty") or 0)
        except Exception:
            qty = 0.0
        if qty <= 0:
            continue
        sold[sku] = sold.get(sku, 0.0) + qty

    items = []
    matched = 0
    gap_cnt = 0
    nostock_cnt = 0
    for sku, total in sold.items():
        own = home_own.get(sku, 0)
        info = sys_info.get(sku)
        if not info:
            continue  # 全系统无档案（服务/权益/特殊订单）→ 无从备货
        matched += 1
        daily = total / WINDOW_DAYS
        sold14 = int(round(total))
        if own > 0:
            suggest = max(0, math.ceil(daily * LEAD_DAYS - own))
            if suggest < 1:
                continue
            gap_cnt += 1
            items.append({
                "code": sku, "name": info["name"], "cat": info["cat"],
                "daily": round(daily, 1), "sold14": sold14, "own": own,
                "suggest": int(suggest), "noStock": False,
            })
        else:
            # 断货类：本店无档案，但有真实销量 → 本地无货热销
            if sold14 < NOSTOCK_MIN_SOLD:
                continue
            if info["cat"] in NOSTOCK_EXCLUDE_CATS:
                continue
            nostock_cnt += 1
            items.append({
                "code": sku, "name": info["name"], "cat": info["cat"],
                "daily": round(daily, 1), "sold14": sold14, "own": 0,
                "suggest": int(math.ceil(daily * LEAD_DAYS)), "noStock": True,
            })
    items.sort(key=lambda x: (-x["suggest"], -x["daily"], x["name"]))
    items = items[:TOP_N]
    stats = {
        "salesSku": len(sold),
        "matched": matched,
        "gapSku": gap_cnt,
        "noStockSku": nostock_cnt,
        "windowStart": start.isoformat(),
        "windowEnd": end.isoformat(),
    }
    return True, items, stats


def fmt_ts(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def main():
    html = open(INDEX, encoding="utf-8").read()
    home_own, sys_info = load_inventory(html)
    print("[inv] 李家村本店有库存SKU=%d  全系统SKU档案=%d" % (len(home_own), len(sys_info)))

    ok, reason, fetched_at, recs = load_sales()
    payload = {
        "updatedAt": fmt_ts(fetched_at) if fetched_at else "",
        "updatedAtFull": datetime.datetime.fromtimestamp(fetched_at).strftime("%Y-%m-%d %H:%M") if fetched_at else "",
        "windowDays": WINDOW_DAYS,
        "leadDays": LEAD_DAYS,
        "windowStart": "",
        "windowEnd": "",
        "stale": not ok,
        "staleReason": reason,
        "items": [],
        "stats": {},
    }
    if ok:
        ok2, items, stats = build_items(home_own, sys_info, recs)
        if ok2:
            payload["items"] = items
            payload["stats"] = stats
            payload["windowStart"] = stats["windowStart"]
            payload["windowEnd"] = stats["windowEnd"]
            print("[sales] 窗口 %s ~ %s：有销量SKU=%d 匹配库存=%d 缺口类=%d 断货类=%d 入榜=%d"
                  % (stats["windowStart"], stats["windowEnd"], stats["salesSku"],
                     stats["matched"], stats["gapSku"], stats["noStockSku"], len(items)))
            for it in items:
                tag = "本地无货" if it["noStock"] else "缺口%d" % it["suggest"]
                print("   - [%s] %s | 日均%.1f 近14天%d 可售%d 建议补%d"
                      % (tag, it["name"], it["daily"], it["sold14"], it["own"], it["suggest"]))
        else:
            payload["staleReason"] = "窗口内无销售明细"
            print("[sales] 警告: 窗口内无销售明细")
    else:
        print("[sales] 警告: %s → 注入空清单（前端提示数据暂不可用）" % reason)

    new_js = "const restockPlanData = %s;" % json.dumps(payload, ensure_ascii=False)
    if RESTOCK_RE.search(html):
        new_html = RESTOCK_RE.sub(lambda m: new_js, html, count=1)
        print("[inject] 已替换 restockPlanData 锚点")
    else:
        m = INSERT_AFTER_RE.search(html)
        if not m:
            raise RuntimeError("找不到 restockPlanData 锚点，也找不到 inventoryData 定义行，无法注入")
        new_html = html[:m.end()] + new_js + "\n" + html[m.end():]
        print("[inject] 锚点缺失，已在 inventoryData 定义后新建（首次注入）")

    if new_html != html:
        with open(INDEX, "w", encoding="utf-8") as f:
            f.write(new_html)
        print("[done] index.html 已注入备货清单（%d 条）" % len(payload["items"]))
    else:
        print("[same] index.html 无变化")


if __name__ == "__main__":
    main()
