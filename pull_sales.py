#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pull_sales.py —— 直连用友「销售分析」报表接口，拉李家村门店销售明细，写入本地缓存。

用途：给 kucunos 工作台「今日该备的货」卡片供数（真实销量 × 库存水位 → 补货缺口）。

接口（与「每日激活查询网页」项目同源，已实测）：
  POST https://c3.yonyoucloud.com/yonbip-mkt-retailweb/report/list
  body: {"billnum":"rm_saleanalysis","page":{"pageIndex":N,"pageSize":5000},
         "queryParams":[{"name":"beginDate","value":"..."},{"name":"endDate","value":"..."}]}
  认证：~/.agent-browser/sessions/yonyou-default.json 里的 cookie（纯 HTTP，免浏览器）
  注意：服务端忽略日期参数，返回全量历史 → 本地按 dDate 过滤/截窗。

去重口径（与晨哥手工导出的《销售分析.xlsx》逐项核对过）：
  1) 剔除单号以 _YD 结尾的预订单/订金变体；
  2) 按 (单号, SKU编码, 序列号) 去重。

缓存：data/sales-cache.json
  {"fetched_at":..., "source":"live", "recordCountRaw":..., "dedupCount":...,
   "homeCount":..., "windowStart":"YYYY-MM-DD", "windowEnd":"YYYY-MM-DD",
   "records":[{"sku":"...", "d":"YYYY-MM-DD", "qty":3, "money":5999.0,
               "neg":"s|r", "emp":"张博晨"}, ...]}
  records 只保留最近 KEEP_DAYS 天的明细（按 单据行 粒度）。
  字段口径（2026-10-23 实测）：
   qty   = fQuantity（退货行为负数，天然冲减净销量）
   money = fNetMoney（实收金额，退货行为负）
   neg   = iNegative（销售/退货）
   emp   = iEmployeeid_name（销售员，供后续业绩分析）

策略：
  - 缓存 < MAX_AGE(4h) → 直接复用，不联网（15 分钟自动化档位不会每次联网）；
  - 联网失败但旧缓存存在 → 保留旧缓存 + 告警，退出码 0（不阻塞工作台更新，
    build_restock.py 会按缓存年龄决定是否展示，绝不拿旧数据冒充新数据）；
  - 联网失败且无任何缓存 → 退出码 1（让调用方记一笔）。
"""
import json, sys, time, os, datetime, urllib.request, urllib.error, ssl

YY_BASE = "https://c3.yonyoucloud.com"
SA_URL = YY_BASE + "/yonbip-mkt-retailweb/report/list"
BILLNUM = "rm_saleanalysis"
DEFAULT_STATE = os.path.expanduser("~/.agent-browser/sessions/yonyou-default.json")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "sales-cache.json")
HOME = "华为李家村万达授权体验店"
SN_FIELD = "oid_userDefine_2419863036093267976"  # 序列号

MAX_AGE = 4 * 3600      # 缓存保鲜时长：4 小时内不重复联网
KEEP_DAYS = 60          # 缓存只保留最近 60 天明细（14 天窗口足够，留余量）
PAGE_SIZE = 5000
TIMEOUT = 180


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def load_cookies(state_path):
    d = json.load(open(state_path))
    return {c["name"]: c["value"] for c in d.get("cookies", [])
            if "yonyoucloud" in c.get("domain", "") and c.get("name") and c.get("value") is not None}


def post_page(ck, page_index, timeout=TIMEOUT):
    xsrf = ck.get("XSRF-TOKEN", "")
    yht = ck.get("yht_access_token", "")
    body = json.dumps({
        "billnum": BILLNUM,
        "page": {"pageIndex": page_index, "pageSize": PAGE_SIZE},
        "queryParams": [
            {"name": "beginDate", "value": "2000-01-01"},
            {"name": "endDate", "value": "2099-12-31"},
        ],
    }).encode("utf-8")
    hdr = {
        "User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": YY_BASE,
        "Referer": YY_BASE + "/",
        "Cookie": "; ".join("%s=%s" % (k, v) for k, v in ck.items()),
        "XSRF-TOKEN": xsrf,
        "yht_access_token": yht,
    }
    req = urllib.request.Request(SA_URL, data=body, headers=hdr, method="POST")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status, json.loads(resp.read())


def dedup_records(records):
    """剔除 _YD 预订单 + 按 (单号, SKU, 序列号) 去重。"""
    seen = set()
    out = []
    for r in records:
        code = str(r.get("code") or "")
        if code.endswith("_YD"):
            continue
        k = (code, str(r.get("productsku_cCode") or ""), str(r.get(SN_FIELD) or ""))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def cache_age():
    try:
        return time.time() - json.load(open(CACHE)).get("fetched_at", 0)
    except Exception:
        return None


def main():
    age = cache_age()
    if age is not None and age < MAX_AGE:
        log("[cache] 销售缓存 %.0f 分钟前刚抓过，跳过联网" % (age / 60))
        print(CACHE)
        return 0

    state = os.environ.get("YONYOU_STATE", DEFAULT_STATE)
    if not os.path.exists(state):
        log("✗ 找不到用友登录态文件: %s" % state)
        return 0 if os.path.exists(CACHE) else 1
    try:
        ck = load_cookies(state)
    except Exception as e:
        log("✗ 登录态文件解析失败: %s" % e)
        return 0 if os.path.exists(CACHE) else 1
    if "yht_access_token" not in ck:
        log("✗ 登录态缺少 yht_access_token（cookie 过期，需重登）")
        return 0 if os.path.exists(CACHE) else 1

    t0 = time.time()
    all_rows, total = [], None
    page = 1
    while page <= 50:
        try:
            st, j = post_page(ck, page)
        except urllib.error.HTTPError as e:
            log("✗ HTTP_%d（登录态失效，需重新登录用友）" % e.code)
            return 0 if os.path.exists(CACHE) else 1
        except Exception as e:
            log("✗ 第 %d 页请求异常: %s" % (page, str(e)[:200]))
            return 0 if os.path.exists(CACHE) else 1
        if st != 200 or j.get("code") != 200:
            log("✗ 接口异常: %s" % str(j.get("message"))[:200])
            return 0 if os.path.exists(CACHE) else 1
        data = j.get("data") or {}
        if total is None:
            total = data.get("recordCount", 0)
        recs = data.get("recordList") or []
        all_rows.extend(recs)
        log("  [页 %d] 本页 %d 行，累计 %d / %s" % (page, len(recs), len(all_rows), total))
        if not recs or len(all_rows) >= (total or 0):
            break
        page += 1

    raw_count = len(all_rows)
    all_rows = dedup_records(all_rows)

    # 门店口径核对：打印仓库分布，确认报表确实是李家村范围（防止账号看到全部门店）
    wh_stat = {}
    for r in all_rows:
        w = str(r.get("iWarehouseid_name") or "")
        wh_stat[w] = wh_stat.get(w, 0) + 1
    top_wh = sorted(wh_stat.items(), key=lambda x: -x[1])[:5]
    log("  [仓库分布Top5] %s" % top_wh)
    home_rows = [r for r in all_rows if str(r.get("iWarehouseid_name") or "") == HOME]
    if not home_rows:
        log("✗ 去重后无李家村记录（仓库名不匹配？），放弃写缓存")
        return 0 if os.path.exists(CACHE) else 1
    if len(home_rows) != len(all_rows):
        log("  [口径] 报表含非李家村记录，已过滤：%d → %d" % (len(all_rows), len(home_rows)))

    # 截窗：只保留最近 KEEP_DAYS 天
    dates = sorted(str(r.get("dDate") or "")[:10] for r in home_rows)
    window_end = dates[-1]
    end_d = datetime.date.fromisoformat(window_end)
    start_d = end_d - datetime.timedelta(days=KEEP_DAYS - 1)
    kept = []
    neg_stat = {"销售": 0, "退货": 0}
    for r in home_rows:
        d = str(r.get("dDate") or "")[:10]
        if d < start_d.isoformat():
            continue
        sku = str(r.get("productsku_cCode") or "").strip()
        if not sku:
            continue
        try:
            qty = float(r.get("fQuantity") or 0)
        except Exception:
            qty = 0.0
        try:
            money = float(r.get("fNetMoney") or r.get("fMoney") or 0)
        except Exception:
            money = 0.0
        neg = str(r.get("iNegative") or "销售")
        neg_stat[neg] = neg_stat.get(neg, 0) + 1
        emp = str(r.get("iEmployeeid_name") or "").strip()
        kept.append({"sku": sku, "d": d, "qty": qty, "money": money,
                     "neg": neg, "emp": emp})

    atomic_write_json(CACHE, {
        "fetched_at": time.time(),
        "source": "live",
        "recordCountRaw": raw_count,
        "dedupCount": len(all_rows),
        "homeCount": len(home_rows),
        "windowStart": start_d.isoformat(),
        "windowEnd": window_end,
        "records": kept,
    })
    log("✓ 销售缓存已写入 %s" % CACHE)
    log("  原始 %d 行 → 去重 %d → 李家村 %d → 最近%d天 %d 行（截至 %s）"
        % (raw_count, len(all_rows), len(home_rows), KEEP_DAYS, len(kept), window_end))
    log("  [正负向] %s" % neg_stat)
    log("  [计时] 耗时 %.1fs" % (time.time() - t0))
    print(CACHE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
