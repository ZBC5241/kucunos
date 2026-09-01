#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xlsx -> csv 转换（李家村库存工作台 V11.4 数据接入）
- 读 ~/Downloads 中最新 现存量_*.xlsx
- 提取 6 列：物料SKU编码 / 物料SKU名称 / *仓库 / *现存量 / *可用量 / *分类名称
- 输出到本目录 现存量_<日期>_默认方案.csv（日期取自 xlsx 文件名）
- 诊断信息走 stderr；仅最终 csv 路径走 stdout（供下游脚本捕获）
"""
import zipfile, re, xml.etree.ElementTree as ET, csv, os, glob, sys, datetime

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
DOWNLOADS = os.path.expanduser("~/Downloads")
HERE = os.path.dirname(os.path.abspath(__file__))


def log(*a):
    print(*a, file=sys.stderr)


def cid(ref):
    m = re.match(r"([A-Z]+)", ref)
    n = 0
    for ch in m.group(1):
        n = n * 26 + ord(ch) - 64
    return n - 1


def cv(c, ss):
    t = c.get("t")
    v = c.find(NS + "v")
    if t == "s" and v is not None:
        return ss[int(v.text)] if int(v.text) < len(ss) else "?"
    if v is not None:
        return v.text
    is_ = c.find(NS + "is")
    if is_ is not None:
        return "".join(x.text or "" for x in is_.iter(NS + "t"))
    return ""


def main():
    files = sorted(glob.glob(os.path.join(DOWNLOADS, "现存量_*.xlsx")),
                   key=os.path.getmtime, reverse=True)
    if not files:
        log("!! 没找到 ~/Downloads/现存量_*.xlsx，本次更新中止")
        sys.exit(1)
    p = files[0]
    log(f"[conv] 用最新 xlsx: {os.path.basename(p)}")
    m = re.search(r"现存量_(\d{8})_默认方案\.xlsx", os.path.basename(p))
    date = m.group(1) if m else datetime.date.today().strftime("%Y%m%d")

    with zipfile.ZipFile(p) as z:
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.parse(z.open("xl/sharedStrings.xml")).getroot()
            ss = ["".join(t.text or "" for t in si.iter(NS + "t"))
                  for si in root.findall(NS + "si")]
        else:
            ss = []
        rows = ET.parse(z.open("xl/worksheets/sheet1.xml")).getroot().findall(".//" + NS + "row")

    data = []
    for r in rows:
        cells = {}
        for c in r.findall(NS + "c"):
            cells[cid(c.get("r"))] = cv(c, ss)
        mx = (max(cells) + 1) if cells else 0
        data.append([cells.get(i, "") for i in range(mx)])

    # row0=标题, row1=表头
    hdr = data[1]

    def find_col(*keys):
        for i, h in enumerate(hdr):
            for k in keys:
                if k in h:
                    return i
        return -1

    ci = find_col("物料SKU编码")
    ni = find_col("物料SKU名称")
    wi = find_col("*仓库", "仓库")
    qi = find_col("*现存量", "现存量")
    ai = find_col("*可用量", "可用量")
    ti = find_col("*分类名称", "分类名称")
    assert -1 not in (ci, ni, wi, qi, ai, ti), "列没找全"

    out = os.path.join(HERE, f"现存量_{date}_默认方案.csv")
    n = 0
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["物料SKU编码", "物料SKU名称", "*仓库", "*现存量", "*可用量", "*分类名称"])
        for r in data[2:]:
            if not "".join(r).strip():
                continue
            w.writerow([r[ci], r[ni], r[wi], r[qi], r[ai], r[ti]])
            n += 1
    log(f"[conv] 写入 {n} 行 -> {out}")
    print(out)  # 仅此行走 stdout，供下游捕获


if __name__ == "__main__":
    main()
