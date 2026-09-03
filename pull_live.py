#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直连用友报表接口拉现存量（V11.6.1 接口升级版）
================================================
替代 update_kucun.py 的「xlsx 导出 → 轮询 → 下载」老路：
在已登录的 c3.yonyoucloud.com 页面里，直接用 CDP Runtime.evaluate 执行 fetch，
POST yonbip-scm-stock/report/list（默认方案 现存量）拿 JSON recordList，
输出与 conv_xlsx.py 完全同格式的 6 列 csv，供 build_v11_inv.py 注入。

字段映射（与 V11.6.1 server.js 一致）：
  code  = productsku_cCode || product_cCode
  name  = productsku_skuName || product_cName
  wh    = warehouse_name || store_name
  q     = currentqty
  a     = availableqty
  cat   = oid_userDefine_2427941397124874250 || productClass_name

前置：Chrome 9223 已起 + 经理账号(18591910491)登录用友。
失败返回非 0，调用方（update_workbench.sh）回退 xlsx 老路。
"""
import json, time, threading, urllib.request, sys, os, datetime, csv
import websocket

HTTP = "http://localhost:9223"
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "data", "query-template.json")
TODAY = datetime.date.today().strftime("%Y%m%d")
OUT = os.path.join(HERE, "现存量_%s_默认方案.csv" % TODAY)

CSV_COLS = ["物料SKU编码", "物料SKU名称", "*仓库", "*现存量", "*可用量", "*分类名称"]


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self._id = 0
        self.events = []
        self._lock = threading.Lock()
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        self.ws.settimeout(1)
        while True:
            try:
                raw = self.ws.recv()
            except Exception:
                time.sleep(0.2)
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            with self._lock:
                self.events.append(msg)

    def cmd(self, method, params=None, session_id=None):
        self._id += 1
        mid = self._id
        m = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            m["sessionId"] = session_id
        self.ws.send(json.dumps(m))
        deadline = time.time() + 40
        while time.time() < deadline:
            with self._lock:
                for i, e in enumerate(self.events):
                    if e.get("id") == mid:
                        self.events.pop(i)
                        return e.get("result", {})
            time.sleep(0.1)
        return {}

    def eval_js(self, expr, session_id=None):
        r = self.cmd("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True, "awaitPromise": True},
                     session_id)
        try:
            return r["result"]["value"]
        except Exception:
            return None


def find_c3(bws):
    ts = bws.cmd("Target.getTargets").get("targetInfos", [])
    for t in ts:
        if t.get("type") == "page" and "c3.yonyoucloud.com" in t.get("url", ""):
            return bws.cmd("Target.attachToTarget",
                           {"targetId": t["targetId"], "flatten": True}).get("sessionId")
    return None


def fetch_once(bws, sid, q):
    expr = (
        "(async()=>{"
        "const r=await fetch(%s,{method:'POST',headers:{'content-type':'application/json'},"
        "credentials:'include',body:%s});"  # body 需双重 stringify（同 V11.6.1）
        "if(!r.ok)return {status:r.status};"
        "return {status:200,data:await r.json()}"
        "})()"
    ) % (json.dumps(q["url"]), json.dumps(json.dumps(q["body"])))
    return bws.eval_js(expr, sid)


def main():
    if not os.path.exists(TEMPLATE):
        print("✗ 缺少 data/query-template.json", file=sys.stderr)
        return 2
    q = json.load(open(TEMPLATE, encoding="utf-8"))

    try:
        ver = json.load(urllib.request.urlopen(HTTP + "/json/version", timeout=5))
        bws = CDP(ver["webSocketDebuggerUrl"])
    except Exception as e:
        print("✗ 无法连 CDP 9223: %s" % e, file=sys.stderr)
        return 3

    sid = find_c3(bws)
    if not sid:
        print("✗ 9223 上无 c3.yonyoucloud.com 页面（经理账号未登录？）", file=sys.stderr)
        return 4

    # 翻页拉全量
    all_recs = []
    page = 1
    total = None
    t0 = time.time()
    while True:
        q["body"]["page"] = {"pageSize": 20000, "pageIndex": page}
        z = fetch_once(bws, sid, q)
        if not z:
            print("✗ 浏览器未返回库存数据（eval 超时/页面已关）", file=sys.stderr)
            return 5
        if z.get("status") in (401, 403):
            print("✗ 用友登录已失效，请在 Chrome 重新登录经理账号", file=sys.stderr)
            return 6
        if z.get("status") != 200:
            print("✗ 库存接口失败（HTTP %s）" % z.get("status"), file=sys.stderr)
            return 7
        j = z.get("data", {})
        if j.get("code") != 200 or not isinstance(j.get("data"), dict) \
                or not isinstance(j["data"].get("recordList"), list):
            print("✗ 数据格式异常: %s" % j.get("message"), file=sys.stderr)
            return 8
        recs = j["data"]["recordList"]
        total = int(j["data"].get("recordCount") or len(all_recs))
        all_recs.extend(recs)
        if len(all_recs) >= total or not recs:
            break
        page += 1
        if page > 50:
            print("⚠️ 翻页超 50 仍不足，强制停止", file=sys.stderr)
            break

    print("[fetch] 耗时 %.1fs  拿到 %d 条 (recordCount=%s, 翻页=%d)"
          % (time.time() - t0, len(all_recs), total, page), file=sys.stderr)

    # 调试：打印首条字段名，便于核对映射
    if all_recs:
        print("[debug] 首条字段: %s" % ", ".join(sorted(all_recs[0].keys())),
              file=sys.stderr)

    def gv(x, *keys):
        for k in keys:
            v = x.get(k)
            if v not in (None, ""):
                return v
        return ""

    n = 0
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for x in all_recs:
            code = str(gv(x, "productsku_cCode", "product_cCode")).strip()
            if not code:
                continue
            name = str(gv(x, "productsku_skuName", "product_cName")).strip()
            wh = str(gv(x, "warehouse_name", "store_name")).strip()
            try:
                qty = int(float(gv(x, "currentqty") or 0))
            except Exception:
                qty = 0
            try:
                avail = int(float(gv(x, "availableqty") or 0))
            except Exception:
                avail = 0
            cat = str(gv(x, "oid_userDefine_2427941397124874250", "productClass_name")).strip()
            w.writerow([code, name, wh, qty, avail, cat])
            n += 1

    print("[csv] 写出 %d 行 -> %s" % (n, OUT), file=sys.stderr)
    print(OUT)  # 仅此行走 stdout，供下游捕获
    return 0


if __name__ == "__main__":
    sys.exit(main())
