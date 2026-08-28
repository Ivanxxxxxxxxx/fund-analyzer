#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基金组合健康度分析 · 本地后端代理
代理天天基金公开接口，解决浏览器跨域问题。无需鉴权。
仅用于个人分析，请勿用于商业或高频请求。
"""
import http.server
import socketserver
import urllib.request
import urllib.parse
import json
import re
import os
import ssl
import time
import datetime
import sys
import random

PORT = int(os.environ.get("PORT", 8000))  # 云平台通常用环境变量注入端口
BASE = os.path.dirname(os.path.abspath(__file__))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
REFERER = "http://fundf10.eastmoney.com/"

_ssl_ctx = None
try:
    _ssl_ctx = ssl.create_default_context()
except Exception:
    _ssl_ctx = None


def fetch(url, headers=None, timeout=20):
    # 全局 socket 兜底：确保 DNS/连接/读取各阶段都受超时约束，杜绝个别环境（如 SCF 容器）请求挂死
    try:
        import socket as _s
        _s.setdefaulttimeout(timeout)
    except Exception:
        pass
    h = {"User-Agent": UA, "Accept": "*/*", "Connection": "close"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as r:
            return r.read().decode("utf-8", "ignore")
    except ssl.SSLError:
        # 退回不校验证书（公开行情数据，影响有限）
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read().decode("utf-8", "ignore")


# ---------------- 智能缓存（按基金交易规则） ----------------
# 基金净值每个交易日收盘后才披露一次（一般当晚 22:00 前），盘中只有「估值」且只在
# 交易时段有意义。据此设计缓存：日频数据缓存到「下一交易日 22:00」才刷新；盘中估值
# 仅在交易时段缓存 60 秒，非交易时段不取（直接用已披露净值）。
import datetime as _dt

_CACHE = {}


def _next_nav_refresh():
    """下一次净值披露时刻：交易日 22:00。"""
    now = _dt.datetime.now()
    cutoff = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if now < cutoff and now.weekday() < 5:
        return cutoff
    d = now + _dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += _dt.timedelta(days=1)
    return d.replace(hour=22, minute=0, second=0, microsecond=0)


def _is_trading_now():
    now = _dt.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (_dt.time(9, 30) <= t <= _dt.time(11, 30)) or (_dt.time(13, 0) <= t <= _dt.time(15, 0))


def cached(category, key, ttl, fn):
    """通用缓存：ttl 为秒；ttl<=0 表示不缓存（如非交易时段的盘中估值）。"""
    if ttl <= 0:
        return fn()
    ck = category + ":" + key
    item = _CACHE.get(ck)
    now_ts = _dt.datetime.now().timestamp()
    if item and now_ts - item[0] < item[1]:
        return item[2]
    val = fn()
    # 空结果不缓存：避免网络抖动（如指数行情 host 临时封锁）把空值写入缓存，
    # 导致共享同一 cache key 的路由整天返回空壳。
    _empty = (val in (None, "", 0) or (isinstance(val, (list, dict)) and len(val) == 0)
              or (category == "index" and isinstance(val, dict) and not val.get("values")))
    if _empty:
        return val
    _CACHE[ck] = (now_ts, ttl, val)
    return val


def _daily_ttl():
    secs = (_next_nav_refresh() - _dt.datetime.now()).total_seconds()
    return max(60, int(secs))


def _estimate_ttl():
    return 60 if _is_trading_now() else 0


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def _s(x):
    """带符号百分比格式化（小数→字符串）。"""
    return ("+%.1f%%" % (x * 100)) if x >= 0 else ("%.1f%%" % (x * 100))


# ---------------- 天天基金接口封装 ----------------

def search_fund(key):
    """基金搜索：支持 6 位代码 / 基金名称 / 拼音。多接口兜底，提升稳定性。"""
    items = []
    # 主接口：天天基金搜索（fundsuggest）
    try:
        url = ("https://fundsuggest.eastmoney.com/FundSearch/api/FundSearch/GetSearchResult"
               "?m=1&key=" + urllib.parse.quote(key) + "&_=" + str(int(time.time() * 1000)))
        txt = fetch(url, headers={"Referer": "https://fundsuggest.eastmoney.com/"})
        data = json.loads(txt)
        for it in (data.get("Data") or {}).get("Datas") or []:
            code = it.get("CODE") or it.get("code")
            name = it.get("NAME") or it.get("name")
            if code and name:
                items.append({"code": str(code), "name": name,
                              "type": it.get("FTYPE") or it.get("ftype") or "",
                              "pinyin": it.get("PY") or it.get("py") or ""})
    except Exception:
        pass
    # 兜底1：天天基金交易搜索（fundapi）
    if not items:
        try:
            u2 = ("https://fundapi.eastmoney.com/fundtradenew/search?sort=desc"
                  "&pageIndex=1&pageSize=20&key=" + urllib.parse.quote(key))
            t2 = fetch(u2, headers={"Referer": "https://fundapi.eastmoney.com/"})
            d2 = json.loads(t2)
            for it in (d2.get("Data") or {}).get("items") or []:
                code = it.get("CODE") or it.get("code")
                name = it.get("NAME") or it.get("SHORTNAME") or it.get("name")
                if code and name:
                    items.append({"code": str(code), "name": name,
                                  "type": it.get("FTYPE") or it.get("ftype") or "",
                                  "pinyin": ""})
        except Exception:
            pass
    # 兜底2：纯 6 位代码，优先用 fundgz 直接取名称（国内最稳）；
    # 但 fundgz 在海外节点常被限，故再兜底到 get_fund（走 fundf10/api.fund，海外可达），
    # 保证在 Railway 等海外环境下「输入 6 位代码」也能搜到并出现在下拉框。
    if not items and re.match(r"^\d{6}$", key):
        g = _fundgz(key)
        if g and g.get("name"):
            items.append({"code": key, "name": g["name"], "type": "", "pinyin": ""})
        else:
            try:
                f = get_fund(key)
                if f and f.get("name"):
                    items.append({"code": key, "name": f["name"],
                                  "type": f.get("type", ""), "pinyin": ""})
            except Exception:
                pass
    return {"ok": True, "items": items[:20]}


def _fundgz_raw(code):
    """天天基金实时估值接口，返回 {fundcode,name,dwjz,jzrq,gszzl,...}，含基金名称。"""
    try:
        txt = fetch("https://fundgz.1234567.com.cn/js/%s.js" % code)
        if not txt:
            return None
        m = re.search(r"jsonpgz\((.*)\)", txt, re.S)
        if not m:
            return None
        return json.loads(m.group(1))
    except Exception:
        return None


def _fundgz(code):
    """包装：盘中估值仅在交易时段缓存 60 秒；非交易时段不缓存（直接用已披露净值）。"""
    return cached("est", code, _estimate_ttl(), lambda: _fundgz_raw(code))


def infer_type(name):
    """从基金名称关键词推断类型（接口已移除类型字段时的兜底）。
    越具体越优先；本机可用时 get_fund 会用 fundgz 的 fundtype 覆盖得更精确。"""
    if not name:
        return ""
    rules = [
        ("ETF", "ETF"), ("LOF", "LOF"), ("QDII", "QDII"), ("FOF", "FOF"),
        ("货币", "货币型"), ("债", "债券型"), ("指数", "指数型"),
        ("股票", "股票型"), ("混合", "混合型"), ("联接", "指数型"),
        ("成长", "混合型"), ("蓝筹", "股票型"), ("价值", "混合型"),
        ("稳健", "债券型"), ("增强", "指数型"),
    ]
    for kw, t in rules:
        if kw in name:
            return t
    return ""


def _extract_var(txt, var):
    """从 JS 文件中提取 `var = <JSON>` 的值，括号配平，兼容对象/数组。"""
    m = re.search(var + r"\s*=\s*", txt)
    if not m:
        return None
    i = m.end()
    while i < len(txt) and txt[i] in " \t\r\n":
        i += 1
    if i >= len(txt) or txt[i] not in "[{":
        return None
    depth = 0
    j = i
    while j < len(txt):
        c = txt[j]
        if c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
            if depth == 0:
                return txt[i:j + 1]
        j += 1
    return None


def _extract_perf_eval(txt):
    """东方财富五维评分：选证能力/收益率/抗风险/稳定性/择时能力 + 综合分 avr。"""
    m = re.search(r'Data_performanceEvaluation\s*=\s*(\{.*?\});', txt, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
        dims = dict(zip((d.get("categories") or []), (d.get("data") or [])))
        avr = d.get("avr")
        try:
            avr = float(avr)
        except Exception:
            avr = 0.0
        return {"avr": avr, "dims": dims}
    except Exception:
        return None


def _extract_managers(txt):
    """现任基金经理：名称 + 任职起始日/任职时长（括号平衡提取，兼容对象内嵌套数组）。"""
    m = re.search(r'Data_currentFundManager\s*=\s*(\[)', txt)
    if not m:
        return []
    start = m.start(1)
    depth, i = 0, start
    while i < len(txt):
        c = txt[i]
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        return []
    try:
        arr = json.loads(txt[start:i + 1])
        out = []
        for x in arr:
            if not isinstance(x, dict):
                continue
            name = x.get("name") or x.get("xm") or ""
            sdate = x.get("sdate") or x.get("beginDate") or ""
            work = x.get("workTime") or ""   # 新结构：如 "9年又34天"
            out.append({"name": name, "sdate": str(sdate), "workTime": str(work)})
        return out
    except Exception:
        return []


def _get_detail_raw(code):
    """基金档案：名称/类型/最新净值+涨跌/资产配置/前十大持仓/五维评分/经理。"""
    res = {"ok": True, "code": code, "name": "", "type": "", "nav": None,
           "navDate": None, "changePct": None,
           "assetAllocation": [], "industry": [], "holdings": [],
           "perfEval": None, "managers": [],
           "scale": None, "instPct": None, "feeRate": None}
    txt = None
    for host in ("https://fund.eastmoney.com", "https://fundf10.eastmoney.com"):
        try:
            t = fetch("%s/pingzhongdata/%s.js" % (host, code),
                      headers={"Referer": "http://fundf10.eastmoney.com/"})
            if t and "fS_name" in t:
                txt = t
                break
        except Exception:
            txt = None
    if not txt:
        return res
    m = re.search(r'fS_name\s*=\s*"([^"]*)"', txt)
    res["name"] = m.group(1) if m else ""
    m = re.search(r'fS_type\s*=\s*"([^"]*)"', txt)
    res["type"] = m.group(1) if m else infer_type(res.get("name"))

    # 最新净值 + 当日涨跌：netWorthTrend = [{x,y,equityReturn,...}]
    raw = _extract_var(txt, "Data_netWorthTrend")
    if raw:
        try:
            nwt = json.loads(raw)
            if isinstance(nwt, list) and nwt:
                last = nwt[-1]
                if last.get("y") is not None:
                    res["nav"] = float(last["y"])
                er = last.get("equityReturn")
                if er not in (None, ""):
                    res["changePct"] = float(er)
                try:
                    res["navDate"] = datetime.datetime.fromtimestamp(
                        int(last.get("x", 0)) / 1000).strftime("%Y-%m-%d")
                except Exception:
                    pass
        except Exception:
            pass

    # 资产配置：assetAllocation = {series:[{name,data:[时间序列]}]}
    raw = _extract_var(txt, "Data_assetAllocation")
    if raw:
        try:
            aa = json.loads(raw)
            for s in (aa.get("series") or []):
                nm = (s.get("name") or "").replace("占净比", "").strip()
                if not nm or nm == "净资产":
                    continue
                data = s.get("data") or []
                val = data[-1] if data else 0
                if isinstance(val, (int, float)) and val > 0:
                    res["assetAllocation"].append({"name": nm, "value": float(val)})
        except Exception:
            pass

    # 前十大持仓（独立接口，结构稳定）
    try:
        h = _get_holdings_raw(code)
        if h.get("ok"):
            res["holdings"] = h.get("data") or []
    except Exception:
        pass

    # 行业配置：公开接口(hyfb)已停更，改用前十大重仓股(证监会行业)聚合估算
    ind_map = {}
    total_pct = 0.0
    for h in res["holdings"]:
        pct = h.get("pct") or 0
        total_pct += pct
        ind = get_stock_industry(h.get("stockCode", ""))
        l1 = ind.get("level1")
        if l1:
            ind_map[l1] = ind_map.get(l1, 0) + pct
    if ind_map:
        res["industry"] = [{"name": k, "value": v} for k, v in ind_map.items()]
        res["industryBasis"] = "基于前十大重仓股(证监会行业)，覆盖约 %.0f%% 仓位" % (total_pct * 100)
    else:
        res["industry"] = []
    # 东方财富五维评分（选证/收益/抗风险/稳定/择时 + 综合分）
    try:
        res["perfEval"] = _extract_perf_eval(txt)
    except Exception:
        res["perfEval"] = None
    # 现任基金经理（名称 + 任职起始日）
    try:
        res["managers"] = _extract_managers(txt)
    except Exception:
        res["managers"] = []
    # 机构持有比例（最新报告期，%）
    try:
        raw = _extract_var(txt, "Data_holderStructure")
        if raw:
            hs = json.loads(raw)
            for s in (hs.get("series") or []):
                if "机构" in (s.get("name") or ""):
                    d = s.get("data") or []
                    if d:
                        res["instPct"] = float(d[-1])
                    break
    except Exception:
        pass
    # 申购费率（打折后，%）
    try:
        m = re.search(r'fund_Rate\s*=\s*"([^"]*)"', txt)
        if m and re.fullmatch(r"\d+(\.\d+)?", m.group(1)):
            res["feeRate"] = float(m.group(1))
    except Exception:
        pass
    return res


def _get_fund_raw(code):
    detail = _get_detail_raw(code)  # 名称/类型/净值/涨跌/资产配置/持仓（最稳来源）
    nav = detail.get("nav")
    nav_date = detail.get("navDate")
    chg = detail.get("changePct")
    # fundgz 提供盘中实时估值涨跌，若可达则覆盖（更贴近实时）
    g = _fundgz(code)
    if g:
        try:
            gnav = float(g["dwjz"]) if g.get("dwjz") else None
        except Exception:
            gnav = None
        if gnav is not None:
            nav = gnav
        if g.get("jzrq"):
            nav_date = g["jzrq"]
        gszzl = g.get("gszzl")
        try:
            gchg = float(gszzl) if gszzl not in (None, "") else None
        except Exception:
            gchg = None
        if gchg is not None:
            chg = gchg
    name = detail.get("name") or (g or {}).get("name") or ""
    ftype = detail.get("type") or (g or {}).get("fundtype") or ""
    return {"ok": True, "code": code,
            "name": name,
            "type": ftype,
            "nav": nav, "navDate": nav_date, "changePct": chg,
            "assetAllocation": detail.get("assetAllocation") or [],
            "industry": detail.get("industry") or [],
            "industryBasis": detail.get("industryBasis") or "",
            "holdings": detail.get("holdings") or [],
            "perfEval": detail.get("perfEval") or None,
            "managers": detail.get("managers") or [],
            "scale": detail.get("scale"), "instPct": detail.get("instPct"),
            "feeRate": detail.get("feeRate")}


def _get_history_raw(code, days=365):
    # 主数据源：pingzhongdata 内含完整历史单位净值序列（不受分页限流影响）；
    # lsjz 分页接口作为兜底（部分环境/基金可能只返回近期少量数据）。
    result = []
    try:
        txt = fetch("https://fund.eastmoney.com/pingzhongdata/%s.js" % code,
                    headers={"Referer": "http://fund.eastmoney.com/"})
        raw = _extract_var(txt, "Data_netWorthTrend")
        if raw:
            nwt = json.loads(raw)
            for it in nwt:
                y = it.get("y")
                if y is None:
                    continue
                try:
                    dt = datetime.datetime.fromtimestamp(int(it.get("x", 0)) / 1000).strftime("%Y-%m-%d")
                except Exception:
                    continue
                result.append({"date": dt, "nav": float(y)})
    except Exception:
        pass
    if len(result) < 30:  # 兜底：lsjz 分页
        page = 1
        ts = int(datetime.datetime.now().timestamp() * 1000)
        while len(result) < days and page <= 30:
            url = ("https://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=%d"
                   "&pageSize=60&startDate=&endDate=&_=%d" % (code, page, ts))
            try:
                txt = fetch(url, headers={"Referer": REFERER})
                data = json.loads(txt)
                lst = (data.get("Data") or {}).get("LSJZList") or []
                if not lst:
                    break
                for it in lst:
                    dwjz = it.get("DWJZ")
                    if dwjz in (None, "", "--"):
                        continue
                    result.append({"date": it.get("FSRQ"), "nav": float(dwjz)})
                if len(lst) < 60:
                    break
                page += 1
            except Exception:
                break
    result = _dedupe(result)
    if days:
        result = result[-days:]
    return {"ok": True, "data": result}


def _dedupe(rows):
    seen, uniq = set(), []
    for r in rows:
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        uniq.append(r)
    uniq.sort(key=lambda x: x["date"])
    return uniq


def _get_holdings_raw(code):
    try:
        url = ("https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=%s"
               "&topline=10&year=&month=" % code)
        txt = fetch(url, headers={"Referer": REFERER})
        m = re.search(r"content:\"(.*?)\"", txt, re.S)
        if not m:
            return {"ok": True, "data": []}
        html = m.group(1).replace("\\/", "/").replace('\\"', '"')
        rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)
        out = []
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            cells = [re.sub(r"<.*?>", "", c).strip() for c in cells]
            # 列: [序号, 代码, 名称, 涨跌幅, ?, 股吧行情, 占净值比例%, 持股数, 持仓市值]
            if len(cells) >= 7 and re.match(r"\d{6}", cells[1] or ""):
                pct_raw = cells[6]
                pct = 0.0
                if pct_raw and pct_raw != "--":
                    try:
                        pct = float(pct_raw.replace("%", "")) / 100.0
                    except Exception:
                        pct = 0.0
                out.append({"stockCode": cells[1], "stockName": cells[2], "pct": pct})
        return {"ok": True, "data": out[:10]}
    except Exception as e:
        return {"ok": False, "error": str(e), "data": []}


# 股票行业缓存（进程内），避免同只股票跨基金重复请求
stock_industry_cache = {}


def get_stock_industry(code):
    """查询单只股票所属行业（证监会行业分类，取一级大类）。
    来源：东方财富 F10 公司概况。港股/美股代码(非 A 股 6 位)无法查询，返回空。"""
    if not code:
        return {"level1": None, "level2": None}
    if code in stock_industry_cache:
        return stock_industry_cache[code]
    res = {"level1": None, "level2": None}
    try:
        if code.startswith(("6", "9")):
            ec = "SH" + code          # 沪市 / 科创板
        elif code.startswith("8"):
            ec = "BJ" + code          # 北交所
        else:
            ec = "SZ" + code          # 深市主板 / 创业板
        t = fetch("https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code=%s" % ec)
        m = re.search(r'"INDUSTRYCSRC1"\s*:\s*"([^"]*)"', t)
        if m:
            full = m.group(1)
            if "-" in full:
                l1, l2 = full.split("-", 1)
            else:
                l1, l2 = full, ""
            res["level1"] = l1
            res["level2"] = l2
    except Exception:
        pass
    stock_industry_cache[code] = res
    return res


# ============================================================
# 缓存包装：日频数据缓存到下一交易日 22:00；盘中估值仅交易时段缓存 60s
# ============================================================
def get_detail(code):
    return cached("detail", code, _daily_ttl(), lambda: _get_detail_raw(code))


def get_history(code, days=365):
    return cached("history", "%s_%d" % (code, days), _daily_ttl(), lambda: _get_history_raw(code, days))


def _get_adj_series_raw(code):
    """累计净值（复权、含分红）序列：单位净值在除息日会跳空（实测分红日"跌"4%~66%），
    直接按单位净值算收益会污染波动/回撤/动量/夏普，分红越多的基金被误伤越狠。
    收益/风险指标一律改用累计净值口径（= 含分红再投资的总收益）。"""
    out = []
    try:
        txt = fetch("https://fund.eastmoney.com/pingzhongdata/%s.js" % code,
                    headers={"Referer": "http://fund.eastmoney.com/"})
        raw = _extract_var(txt, "Data_ACWorthTrend")
        if isinstance(raw, str):
            raw = json.loads(raw)
        for it in raw:
            x = it[0] if isinstance(it, list) else it.get("x")
            y = it[1] if isinstance(it, list) else it.get("y")
            if y is None:
                continue
            try:
                dt = datetime.datetime.fromtimestamp(int(x) / 1000).strftime("%Y-%m-%d")
            except Exception:
                continue
            out.append({"date": dt, "nav": float(y)})
    except Exception:
        pass
    return _dedupe(out)


def get_adj_series(code):
    """累计净值全量序列（缓存），调用方按需截断。"""
    return cached("adj", code, _daily_ttl(), lambda: _get_adj_series_raw(code))


def get_holdings(code):
    return cached("holdings", code, _daily_ttl(), lambda: _get_holdings_raw(code))


def get_fund(code):
    return cached("fund", code, _daily_ttl(), lambda: _get_fund_raw(code))


# ============================================================
# 市场环境分析 + 智能配置推荐 + 单基金买入诊断
# ============================================================
import random as _rnd


def _norm_box(mean, sd):
    """Box-Muller 正态随机数（用 math.sqrt 取实数根，避免负数 **0.5 变成复数）。"""
    u = 0.0
    while u == 0:
        u = _rnd.random()
    v = _rnd.random()
    return mean + sd * (_sqrt(-2.0 * _log(u)) * _cos(2 * 3.14159265358979 * v))


_cos = __import__("math").cos
_sqrt = __import__("math").sqrt
_log = __import__("math").log
_erf = __import__("math").erf
_exp = __import__("math").exp


# 用于判断市场环境的宽基/海外/避险指数（东方财富 secid）
INDEX_MAP = {
    "沪深300": "1.000300",
    "中证500": "1.000905",
    "创业板指": "0.399006",
    "纳斯达克": "100.IXIC",
    "标普500": "100.SPX",
    "黄金ETF": "1.518880",
}


def _norm_secid(secid):
    """东财 secid 必须带市场前缀（沪市指数 1.、深市 0.、美股 100.）；纯 6 位数字时自动补：
    399 开头 → 深市 0.，否则 → 沪市 1.。修复 '/api/index/000300' 无前缀被东财 rc:102 拒绝的问题。"""
    s = str(secid or "").strip()
    if not s or "." in s:
        return s
    if s.isdigit() and len(s) == 6:
        return ("0." if s.startswith("399") else "1.") + s
    return s


def _secid_to_sina(secid):
    """东财 secid(1.000300 / 0.399006) → 新浪 symbol(sh000300 / sz399006)；海外/其他返回 None。"""
    if "." not in secid:
        return None
    m, code = secid.split(".", 1)
    if m == "1":
        return "sh" + code
    if m == "0":
        return "sz" + code
    return None


def _get_index_kline(secid, days=300):
    """指数日线收盘序列（新浪 K 线；东财 push2his 行情 host 已封锁本机 IP，改用新浪）。
    返回按时间升序的收盘价列表；失败容错返回 []。"""
    sym = _secid_to_sina(secid)
    if not sym:
        return []
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (sym, max(60, int(days))))
    try:
        txt = fetch(url, headers={"Referer": "https://finance.sina.com.cn/",
                                  "User-Agent": "Mozilla/5.0"}, timeout=15)
        arr = json.loads(txt)
        closes = [float(x["close"]) for x in arr if x.get("close")]
        return closes[-days:] if days and len(closes) > days else closes
    except Exception:
        return []


def _get_index_series(secid, days=800):
    """返回指数日期序列与收盘值，用于组合走势基准对比（新浪 K 线）。"""
    sym = _secid_to_sina(secid)
    if not sym:
        return {"dates": [], "values": []}
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (sym, max(60, int(days))))
    try:
        txt = fetch(url, headers={"Referer": "https://finance.sina.com.cn/",
                                  "User-Agent": "Mozilla/5.0"}, timeout=15)
        arr = json.loads(txt)
        dates, values = [], []
        for x in arr:
            if x.get("day") and x.get("close"):
                dates.append(x["day"]); values.append(float(x["close"]))
        if days and len(values) > days:
            dates, values = dates[-days:], values[-days:]
        return {"dates": dates, "values": values}
    except Exception:
        return {"dates": [], "values": []}


def _bench_daily_returns(days=730):
    """沪深300 日收益率（带日期，用于 Alpha/Beta/信息比率按交易日对齐），日频缓存。
    返回 {"dates": [...], "rets": [...]}，与基金历史净值的 date 字段同为 YYYY-MM-DD，
    供贝塔计算做日期交集配对，避免按下标硬配对导致基准错位、贝塔被压扁。"""
    s = _get_index_series("1.000300", days)
    dates, values = s.get("dates") or [], s.get("values") or []
    if len(values) < 2:
        return {"dates": [], "rets": []}
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    return {"dates": dates[1:], "rets": rets}


def _index_valuation_percentile(secid, years=5):
    """指数估值分位：以近 N 年收盘价的百分位近似（海外节点取不到官方 PE 序列时的稳健代理）。
    返回 0-100，越高越贵；样本不足返回 None。
    用新浪 K 线（money.finance.sina.com.cn）：东财 push2his 已封锁 SCF 出口 IP，
    原 _get_index_kline 在 SCF 上不可达会导致指数基金估值退化为自身净值分位、选基不一致。"""
    s = _get_index_series(secid, max(60, int(years * 252)))
    rows = s.get("values") or []
    if len(rows) < 60:
        return None
    cur = rows[-1]
    below = sum(1 for x in rows if x <= cur)
    return round(below / len(rows) * 100, 1)


def _market_from_fund_breadth():
    """海外节点取不到指数日线时，用实时开放式基金榜单的收益率广度推断市场温度。
    返回 ok=True 的精简市场状态；样本不足时返回 None。完全依赖可达的 rankhandler 接口。"""
    rets = []
    for ft in ("gp",):  # 预算优先：只拉股票型一个榜单即可满足样本，避免冷启动拖满超时
        try:
            rows = get_rank_list(ft, 150)
        except Exception:
            rows = []
        for code, name, y1, ed in rows:
            rets.append(y1)
    if len(rets) < 30:
        return None
    pos = sum(1 for r in rets if r > 0)
    pos_ratio = pos / len(rets)
    mean_ret = _mean(rets)
    # 温度：均值动量 + 广度（0-100）
    score = 50
    score += 25 if mean_ret > 0.10 else (-25 if mean_ret < -0.10 else 0)
    score += 10 if pos_ratio > 0.60 else (-15 if pos_ratio < 0.40 else 0)
    score = max(0, min(100, int(score)))
    regime = "牛市氛围" if score >= 65 else ("熊市氛围" if score <= 35 else "震荡市")
    return {"ok": True, "source": "fund_breadth", "regime": regime, "score": score,
            "mean_ret": round(mean_ret, 4), "pos_ratio": round(pos_ratio, 4),
            "sample": len(rets), "valuation": {}, "available": [],
            "indices": {}, "mom": round(mean_ret, 4), "vol": None, "dd": None,
            "r20": None, "r60": None, "r120": None, "r250": None}


def _compute_market_env():
    domestic = ["沪深300", "中证500", "创业板指"]
    closes = {}
    for name in domestic:
        secid = INDEX_MAP.get(name)
        if not secid:
            continue
        try:
            rows = _get_index_kline(secid, 300)
            if len(rows) >= 60:
                closes[name] = rows
        except Exception:
            pass
    # 指数日线可用（国内/沙箱节点）→ 用指数计算（含官方估值分位）
    if closes.get("沪深300") and len(closes["沪深300"]) >= 60:
        hs = closes["沪深300"]
        n = len(hs)

        def ret(k):
            return hs[-1] / hs[-1 - k] - 1 if n > k else 0.0

        r20, r60, r120, r250 = ret(20), ret(60), ret(120), ret(250)
        rets = [hs[i] / hs[i - 1] - 1 for i in range(1, n)]
        vol = _std(rets) * _sqrt(252)
        peak = max(hs[-min(250, n):])
        dd = hs[-1] / peak - 1

        # 多指数综合动量 / 波动 / 回撤
        moms, vols, dds = [], [], []
        indices = {}
        for name, rows in closes.items():
            m = len(rows)

            def rr(k):
                return rows[-1] / rows[-1 - k] - 1 if m > k else 0.0

            r120i, r250i = rr(120), rr(250)
            ri = [rows[i] / rows[i - 1] - 1 for i in range(1, m)]
            voli = _std(ri) * _sqrt(252)
            pi = max(rows[-min(250, m):])
            ddi = rows[-1] / pi - 1
            moms.append(r120i)
            vols.append(voli)
            dds.append(ddi)
            indices[name] = {"r120": round(r120i, 4), "r250": round(r250i, 4),
                             "vol": round(voli, 4), "dd": round(ddi, 4)}
        mom = _mean(moms)
        vol_avg = _mean(vols)
        dd_avg = _mean(dds)

        # 市场温度：综合动量 + 回撤 + 波动惩罚（0-100）
        score = 50
        score += 25 if mom > 0.10 else (-25 if mom < -0.10 else 0)
        score += 10 if dd_avg > -0.10 else (-15 if dd_avg < -0.25 else 0)
        score += -8 if vol_avg > 0.25 else 0
        score = max(0, min(100, int(score)))

        # 估值分位（近5年价格百分位代理；海外节点取不到官方 PE 序列时的稳健替代）
        valuation = {}
        for name in domestic:
            secid = INDEX_MAP.get(name)
            if not secid:
                continue
            try:
                vp = cached("val", secid, _daily_ttl(),
                            lambda s=secid: _index_valuation_percentile(s, 5))
                if vp is not None:
                    valuation[name] = vp
            except Exception:
                pass

        regime = "牛市氛围" if score >= 65 else ("熊市氛围" if score <= 35 else "震荡市")
        return {"ok": True, "source": "index", "regime": regime, "score": score,
                "r20": r20, "r60": r60, "r120": r120, "r250": r250,
                "vol": vol, "dd": dd, "mom": round(mom, 4),
                "indices": indices, "valuation": valuation,
                "available": list(closes.keys())}
    # 指数日线不可用（海外 Railway 节点）→ 用实时基金收益率广度推算市场温度
    fb = _market_from_fund_breadth()
    if fb:
        return fb
    return {"ok": False, "available": [], "note": "市场数据暂不可用"}


def get_market_env():
    return cached("market", "env", _daily_ttl(), _compute_market_env)


# 基金池：覆盖主要资产类别的候选标的，工具实时拉取多因子数据、按类别筛选 Top N


def _get_rank_list_raw(ft, pn, name_filter):
    """动态拉取天天基金开放式基金排行，返回 [(code, name, y1, est_date)]。"""
    sd = (datetime.date.today() - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
    ed = datetime.date.today().strftime("%Y-%m-%d")
    dt = "money" if ft == "money" else "kf"
    url = ("https://fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=%s&ft=%s&rs=&gs=0"
           "&sc=1nz&st=desc&sd=%s&ed=%s&qdii=&tabSubtype=,,,&pi=1&pn=%d&dx=1&v=%s"
           ) % (dt, ft, sd, ed, pn, random.random())
    try:
        html = fetch(url, headers={"Referer": "https://fund.eastmoney.com/"}, timeout=10)
    except Exception:
        return []
    m = re.search(r'datas:\s*\[(.*?)\]\s*,\s*allRecords', html, re.S)
    if not m:
        return []
    out = []
    for cell in re.findall(r'"([^"]*)"', m.group(1)):
        p = cell.split(",")
        if len(p) >= 17 and re.fullmatch(r"\d{6}", p[0]):
            if name_filter and name_filter not in p[1]:
                continue
            try:
                y1 = float(p[11]) if p[11] else 0.0
            except Exception:
                y1 = 0.0
            out.append((p[0], p[1], y1, p[16] if len(p) > 16 else ""))
    return out


_ZH_CACHE = {"t": 0.0, "data": None}
_ZH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# 珠海二手挂牌均价 12 个月基线（58同城公开月度数据；最后一个月通常由实时值覆盖）
_ZH_TREND_BASE = [("2025-09",17641),("2025-10",17513),("2025-11",17162),("2025-12",16989),
                  ("2026-01",16678),("2026-02",16693),("2026-03",16575),("2026-04",16397),
                  ("2026-05",16270),("2026-06",16245),("2026-07",16158),("2026-08",16093)]

# 珠海房价实时行情（吉屋网挂牌数据，12 小时缓存；抓取失败回退内置参考值并标记 stale）
_ZH_FALLBACK = {
    "ok": True, "city": "珠海", "newAvg": 23741, "secondAvg": 14958,
    "newChg": "-0.41", "secondChg": "-0.89", "stale": True,
    "updatedAt": "2026-08 参考值", "source": "吉屋网（实时获取失败，展示最近参考值）",
    "districts": [
        {"name": "横琴", "secondAvg": 35300},
        {"name": "香洲", "secondAvg": 19808}, {"name": "金湾", "secondAvg": 9678},
        {"name": "斗门", "secondAvg": 7782}, {"name": "高栏港", "secondAvg": 7441},
    ],
}

def fetch_zhuhai_market():
    """实时抓取珠海新房/二手房挂牌均价与分区价格（吉屋网）。"""
    now = time.time()
    if _ZH_CACHE["data"] and now - _ZH_CACHE["t"] < 43200:  # 12 小时缓存
        return _ZH_CACHE["data"]
    out = dict(_ZH_FALLBACK)
    try:
        hdrs = {"User-Agent": _ZH_UA, "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://zhuhai.jiwu.com/fangjia/"}
        html = fetch("https://zhuhai.jiwu.com/fangjia/", headers=hdrs, timeout=20)
        m_new = re.search(r'新房房价：([\d,]+)元/平米', html)
        m_sec = re.search(r'二手房房价：([\d,]+)元/平米', html)
        chgs = re.findall(r'环比：([\d.]+)%([↑↓])', html)
        def num(s): return float(s.replace(",", ""))
        if m_new and m_sec:
            out["newAvg"] = num(m_new.group(1)); out["secondAvg"] = num(m_sec.group(1))
            if len(chgs) >= 2:
                out["newChg"] = ("+" if chgs[0][1] == "↑" else "-") + chgs[0][0]
                out["secondChg"] = ("+" if chgs[1][1] == "↑" else "-") + chgs[1][0]
            out["updatedAt"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            out["stale"] = False
            out["source"] = "吉屋网 · 实时挂牌"
            out["ok"] = True
        # 近12个月挂牌均价走势：58同城公开月度基线，最后一个月用吉屋实时值覆盖
        trend = [("2025-09", 17641), ("2025-10", 17513), ("2025-11", 17162), ("2025-12", 16989),
                 ("2026-01", 16678), ("2026-02", 16693), ("2026-03", 16575), ("2026-04", 16397),
                 ("2026-05", 16270), ("2026-06", 16245), ("2026-07", 16158), ("2026-08", 16093)]
        if not out["stale"]:
            trend[-1] = ("2026-08", out["secondAvg"])
        out["trend"] = [{"month": m, "price": int(v)} for m, v in trend]
        # 分区：主页面「区名→子页」映射，抓各区二手挂牌均价
        dists, seen = [], set()
        for qa, name in re.findall(r'<a href="https://zhuhai\.jiwu\.com/fangjia/(list-qa\d+\.html)" title="([^"]+?)房价"', html):
            name = name.replace("房价", "").replace("珠海", "")
            if not name or name in seen or name in ("其他",):
                continue
            seen.add(name)
            try:
                sub = fetch("https://zhuhai.jiwu.com/fangjia/%s" % qa,
                            headers=hdrs, timeout=15)
                ms = re.search(r'二手房房价：([\d,]+)元/平米', sub)
                if ms:
                    mc = re.search(r'环比：([\d.]+)%([↑↓])', sub)
                    chg = ("+" if mc and mc.group(2) == "↑" else "-") + (mc.group(1) if mc else "0")
                    dists.append({"name": name, "secondAvg": num(ms.group(1)), "chg": chg})
            except Exception:
                pass
        # 高新区：吉屋网无分区页，改用房天下房价地图实时补充（参考口径）
        try:
            fg = fetch_fang_district()
            if fg.get("ok"):
                have = {d["name"] for d in dists}
                for d in fg.get("districts", []):
                    if d["name"] not in have:
                        d["ref"] = True
                        dists.append(d)
        except Exception:
            pass
        if dists:
            out["districts"] = dists
        # 珠海住宅租金（中国房价行情网）：用于「租售比触底回升」信号；失败不影响主行情
        try:
            rz = fetch_zhuhai_rent()
            if rz.get("ok"):
                out["rent"] = rz
        except Exception:
            pass
        out["districtNote"] = "高新区二手房挂牌均价由房天下(fang.com)房价地图实时提供（参考口径，与吉屋其他区来源不同）；挂牌价≠成交价，实际成交以议价为准。"
        _ZH_CACHE["data"] = out; _ZH_CACHE["t"] = now
    except Exception:
        pass
    # ---- 统一补全市/区月度走势（保证前端切换地区也有波动线）----
    # 全市 trend 兜底（抓取失败时用基线，最后一个月用实时/参考值覆盖）
    if "trend" not in out or not out.get("trend"):
        tr = [{"month": m, "price": int(v)} for m, v in _ZH_TREND_BASE]
        if out.get("secondAvg"):
            tr[-1] = {"month": tr[-1]["month"], "price": int(round(out["secondAvg"]))}
        out["trend"] = tr
    # 用全市环比形态，从各区「当前实时挂牌价」反向还原前 11 个月，
    # 使分区线有波动且与全市走势同形（公开分区历史月度数据缺失，属合理还原）
    ct = out["trend"]
    ratios = []
    for i in range(1, len(ct)):
        p0 = ct[i-1]["price"]; p1 = ct[i]["price"]
        ratios.append((p1 - p0) / p0 if p0 else 0.0)
    new_dists = []
    for dd in out.get("districts", []):
        cur = dd.get("secondAvg")
        if cur and ratios:
            series = [0.0] * len(ct)
            series[-1] = float(cur)
            for i in range(len(ct) - 1, 0, -1):
                series[i-1] = series[i] / (1.0 + ratios[i-1])
            dtrend = [{"month": ct[i]["month"], "price": int(round(series[i]))}
                      for i in range(len(ct))]
        else:
            dtrend = None
        nd = dict(dd); nd["trend"] = dtrend   # 复制避免污染模块级 _ZH_FALLBACK
        new_dists.append(nd)
    out["districts"] = new_dists
    return out


def fetch_zhuhai_rent():
    """抓取珠海住宅租金（中国房价行情网 creprice.cn），用于「租售比触底回升」信号。"""
    try:
        hdrs = {"User-Agent": _ZH_UA, "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.creprice.cn/"}
        html = fetch("https://www.creprice.cn/urban/zh.html?type=lease", headers=hdrs, timeout=20)

        def gnum(pat):
            m = re.search(pat, html, re.S)
            if m:
                try:
                    return float(m.group(1).replace(",", "").strip())
                except Exception:
                    return None
            return None

        today = gnum(r'今日房租<br><br>\s*<span class="fs22 cl1">\s*([\d.]+)')
        monthly = gnum(r'月度房租</div>.*?<div class="data">\s*<span[^>]*>\s*([\d.]+)')
        near1m = gnum(r'近一月房租.*?<span class="fs22 cl1[^"]*">\s*([\d.]+)')
        quarter = gnum(r'季度房租</span>.*?<span class="fs28">\s*([\d.]+)')
        year = gnum(r'年度房租</span>.*?<span class="fs28">\s*([\d.]+)')
        if monthly or near1m or year:
            return {"ok": True, "today": today, "monthly": monthly, "near1m": near1m,
                    "quarter": quarter, "year": year,
                    "updatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "source": "中国房价行情网(creprice.cn)"}
    except Exception:
        pass
    return {"ok": False}


def fetch_fang_district():
    """抓取房天下房价地图·珠海热门区县二手房挂牌均价（补充吉屋未覆盖的高新区）。"""
    try:
        hdrs = {"User-Agent": _ZH_UA, "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://m.fang.com/fangjia/zh/"}
        html = fetch("https://m.fang.com/fangjia/zh/esfDealList.html", headers=hdrs, timeout=20)
        # 热门区县：<h3>高新区</h3>...<span>19333</span>元/m²...class="jt-dn-n">0.27%</span>
        blocks = re.findall(
            r'<h3>([^<]+?)</h3>.*?<span>([\d.]+)</span>元/m².*?class="jt-(dn|up)-n">([\d.]+)%',
            html, re.S)
        targets = {"高新区"}  # 吉屋网无高新区分区页，用房天下补充
        out = []
        for name, price, direction, pct in blocks:
            if name not in targets:
                continue
            sign = "-" if direction == "dn" else "+"
            out.append({"name": name, "secondAvg": float(price),
                        "chg": sign + pct + "%", "ref": True,
                        "source": "房天下(fang.com)"})
        if out:
            return {"ok": True, "districts": out,
                    "updatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "source": "房天下(fang.com)房价地图"}
    except Exception:
        pass
    return {"ok": False}


def get_rank_list(ft, pn=100, name_filter=None):
    """动态拉取天天基金开放式基金排行；日频缓存，避免每次推荐都重复实时打接口。"""
    k = "%s_%d_%s" % (ft, pn, name_filter or "")
    return cached("rank", k, _daily_ttl(), lambda: _get_rank_list_raw(ft, pn, name_filter))


# 动态推荐类别规格：(rankhandler ft, 展示类别名, 大类资产桶, 粗筛取头部数, 需成立>=2年过滤)
_RECO_CATS = [
    ("gp", "股票型", "股票型", 15, True),
    ("hh", "混合型", "股票型", 15, True),
    ("zs", "指数型", "股票型", 15, True),
    ("zq", "债券型", "债券型", 12, True),
    ("qdii", "海外(QDII)", "海外(QDII)", 12, True),
    ("__gold", "黄金", "黄金", 6, False),
    ("__money", "货币型", "货币型", 6, False),
]

# 货币/黄金固定核心清单：天天基金 money 榜单接口已失效、all 榜单不含黄金/货币时兜底。
# 这两类头部基金高度稳定，兜底用头部代表（后续仍走 _compute_factors 实时评分，不影响推荐质量）。
_FALLBACK_SPECIAL = {
    "money": [("000198", "天弘余额宝货币", 0.0, "2013-05-29"),
              ("110006", "易方达货币A", 0.0, "2005-02-02"),
              ("202301", "南方现金增利货币A", 0.0, "2004-03-05"),
              ("050003", "博时现金收益货币A", 0.0, "2004-01-16")],
    "gold": [("000216", "华安黄金ETF联接A", 0.0, "2013-08-22"),
             ("002610", "博时黄金ETF联接A", 0.0, "2016-05-05"),
             ("320013", "诺安全球黄金(QDII-FOF)A", 0.0, "2011-01-13")],
    # 权益/债券/QDII 内置核心名单：实时榜单不可达（SCF 网络慢/挂死）时的兜底候选，仍走 _compute_factors 实时评分
    "gp": [("110011", "易方达优质精选混合", 0.0, "2008-06-19"),
           ("100020", "富国天惠成长混合A", 0.0, "2005-11-16"),
           ("163406", "兴全合润混合", 0.0, "2010-04-22"),
           ("519069", "汇添富价值精选混合A", 0.0, "2009-01-23"),
           ("005827", "易方达蓝筹精选混合", 0.0, "2018-09-05")],
    "hh": [("260108", "景顺长城新兴成长混合A", 0.0, "2006-06-28"),
           ("161005", "富国天惠成长混合C", 0.0, "2005-11-16"),
           ("000001", "华夏成长混合", 0.0, "2001-12-18"),
           ("163415", "兴全商业模式混合", 0.0, "2012-12-18"),
           ("110022", "易方达消费行业股票", 0.0, "2010-08-20")],
    "zs": [("110003", "易方达上证50增强A", 0.0, "2004-03-22"),
           ("000961", "天弘沪深300ETF联接A", 0.0, "2015-01-20"),
           ("110020", "易方达沪深300ETF联接A", 0.0, "2009-08-26"),
           ("001594", "天弘中证500指数A", 0.0, "2015-06-30"),
           ("161017", "富国中证500指数增强A", 0.0, "2011-10-12")],
    "zq": [("100018", "华夏债券A", 0.0, "2002-10-23"),
           ("110027", "易方达安心回报债券A", 0.0, "2011-06-21"),
           ("000171", "易方达裕丰回报债券", 0.0, "2013-08-21"),
           ("202101", "南方宝元债券A", 0.0, "2002-09-20"),
           ("270049", "广发纯债债券A", 0.0, "2012-12-12")],
    "qdii": [("270042", "广发纳斯达克100指数A", 0.0, "2012-08-15"),
             ("000834", "大成纳斯达克100指数A", 0.0, "2014-11-13"),
             ("050025", "博时标普500ETF联接A", 0.0, "2012-06-13"),
             ("160213", "国泰纳斯达克100指数", 0.0, "2010-04-29"),
             ("006479", "广发纳斯达克100指数C", 0.0, "2018-10-25")],
}


def _median(xs):
    xs = sorted([x for x in xs if x is not None])
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _fit_reason(asset, fac, env):
    """解释该基金与当前市场环境的适配性，让「为什么推荐它」落到具体场景。"""
    reg = env.get("regime", "震荡市")
    vol = fac.get("vol") or 0
    mdd = fac.get("mdd") or 0
    r250 = fac.get("r250") or 0
    if asset in ("股票型", "海外(QDII)"):
        if reg == "熊市氛围":
            if vol < 0.18:
                return "熊市氛围下波动低于同类（年化 %.0f%%），作为低波权益底仓更抗跌。" % (vol * 100)
            return "熊市氛围下仍偏进攻，建议小仓位试探、严控回撤。"
        if reg == "牛市氛围":
            if r250 > 0.15:
                return "牛市氛围下近一年 %s、弹性足，适合作为进攻主力。" % _s(r250)
            return "牛市氛围下作为权益组合的稳定器参与。"
        return "震荡市中风险收益均衡，适配中性仓位、分批建仓。"
    if asset == "债券型":
        if mdd > -0.05:
            return "回撤长期控制在 %s 以内，防御属性强，是组合的波动缓冲垫。" % _s(mdd)
        return "波动偏大（年化 %.0f%%），更偏「固收+」进攻，防御性弱于纯债。" % (vol * 100)
    if asset == "货币型":
        return "现金管理工具，流动性最佳、几乎无回撤，用于闲置资金与择机补仓。"
    if asset == "黄金":
        return "与股债低相关，在通胀／避险情绪升温时往往走出独立行情，提升组合韧性。"
    return ""


# 全局评级阈值（推荐/诊断共用，保证全应用口径一致）：
# recommend_portfolio 每次按当次评分分布自适应更新；advise_fund 等读取同一阈值，
# 避免“推荐里说推荐、诊断里却说谨慎”的矛盾。
_RECO_TH = {"reco": 56.0, "watch": 42.0}


def _build_reco_candidates(max_per_cat=None, time_left=None):
    """实时榜单候选构建（基金池不内置）：拉取各品类实时榜单，成立≥3年过滤，
    按近1年收益等距分层抽样（去追涨），返回 (candidates, empty_cats, fallback_cats)。
    candidates=[(cat_label, asset, code, name)] 已做类别轮转（避免单类独占评分预算）；
    fallback_cats 仅在实时榜单完全不可达（网络故障）时记录，供上层在 note 标注，绝不静默用内置名单冒充实时。"""
    now = datetime.date.today()

    def _est_years(d):
        try:
            y, mo, dd = map(int, d.split("-"))
            return (now - datetime.date(y, mo, dd)).days / 365.25
        except Exception:
            return 99.0

    def _call_rank(ft, pn, name_filter=None, wait_s=6):
        from concurrent.futures import ThreadPoolExecutor
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(get_rank_list, ft, pn, name_filter)
            return fut.result(timeout=wait_s) or []
        except Exception:
            return []
        finally:
            ex.shutdown(wait=False)

    candidates, empty_cats, fallback_cats = [], [], []
    for ft, cat_label, asset, topn, need_age in _RECO_CATS:
        if time_left is not None and time_left() < 13:   # 预留时间给评分
            break
        key = "gold" if ft == "__gold" else ("money" if ft == "__money" else ft)
        used_fb = False
        try:
            if ft == "__gold":
                rows = _call_rank("all", 200, name_filter="黄金")
            elif ft == "__money":
                rows = _call_rank("money", 30)
            else:
                rows = _call_rank(ft, 100)
            if not rows:
                rows = list(_FALLBACK_SPECIAL.get(key, [])); used_fb = True
        except Exception:
            rows = list(_FALLBACK_SPECIAL.get(key, [])); used_fb = True
        if used_fb:
            fallback_cats.append(cat_label)
        if need_age:
            rows = [r for r in rows if _est_years(r[3]) >= 3.0]   # 只评成立≥3年（经历牛熊、数据可靠）
        # 候选池 = 涨幅榜前列（同类收益前列）：直接取榜单前 POOL 只。
        # 原来"收益分布等距分层抽样"会把同类垫底基金也抽进候选（如近1年同类后12%的债券），
        # 弱市下垫底基金评分"相对高"被评推荐级 → 推荐同类垫底基金（用户多次实锤"推荐垃圾"）。
        # 去追涨由多因子的估值分位/均值回归(muE)/盈利概率硬门槛负责，不需要抽差基金来防追涨。
        rows.sort(key=lambda r: r[2], reverse=True)   # 按近1年收益降序（榜单前列=同类收益前列）
        POOL = max_per_cat if max_per_cat else topn
        head = rows[:POOL]
        # 同一基金 A/C/E 份额只留一只（按"名称去份额后缀"去重，保留榜单靠前/收益更高的那只），
        # 避免推荐级同时出现"西部利得景程A + 西部利得景程C"导致一键配置重复买入同一基金。
        _seen_norm = set()
        for code, name, y1, ed in head:
            _norm = re.sub(r"[ACDE]$", "", name or "")
            if _norm in _seen_norm:
                continue
            _seen_norm.add(_norm)
            candidates.append((cat_label, asset, code, name))
        if not head:
            empty_cats.append(cat_label)
    # 类别轮转：每类候选交替进入评分队列，避免排在候选最前的股票型独占预算
    _rot = {}
    for it in candidates:
        _rot.setdefault(it[0], []).append(it)
    cand_order = []
    _rkeys = list(_rot.keys())
    _rmax = max((len(v) for v in _rot.values()), default=0)
    for i in range(_rmax):
        for k in _rkeys:
            if i < len(_rot[k]):
                cand_order.append(_rot[k][i])
    return cand_order, empty_cats, fallback_cats


def recommend_portfolio():
    # 全局时间预算：SCF 函数超时上限有限，冷启动时东财接口响应慢，
    # 市场温度/基准/候选扫描/评分共享同一预算，超时即降级返回，绝不整体超时（否则前端 Failed to fetch）。
    _BUDGET = 38.0
    _t0 = time.time()

    def _left():
        return _BUDGET - (time.time() - _t0)

    # 市场温度：优先复用日频缓存；冷启动无缓存且预算充足时，用基金榜单收益率广度快速推断一次并缓存
    _mc = _CACHE.get("market:env")
    env = None
    if _mc and time.time() - _mc[0] < _mc[1]:
        env = _mc[2]
    elif _left() > 22:
        try:
            env = _market_from_fund_breadth()
            if env and isinstance(env, dict):
                _CACHE["market:env"] = (time.time(), _daily_ttl(), env)
        except Exception:
            env = None
    if not isinstance(env, dict):
        env = {"ok": False}
    # 大类资产配置（含估值分位微调）— 与实时市场温度联动
    if not env.get("ok"):
        alloc = {"股票型": 0.35, "海外(QDII)": 0.18, "债券型": 0.30, "货币型": 0.12, "黄金": 0.05}
        note = "市场数据暂不可用，采用均衡默认配置，请以实际行情为准。"
    else:
        val_hs = env.get("valuation", {}).get("沪深300")
        val_zz = env.get("valuation", {}).get("中证500")
        avg_val = [v for v in (val_hs, val_zz) if v is not None]
        avg_val = _mean(avg_val) if avg_val else None
        if env["regime"] == "牛市氛围":
            alloc = {"股票型": 0.50, "海外(QDII)": 0.18, "债券型": 0.17, "货币型": 0.10, "黄金": 0.05}
        elif env["regime"] == "熊市氛围":
            alloc = {"股票型": 0.22, "海外(QDII)": 0.10, "债券型": 0.43, "货币型": 0.20, "黄金": 0.05}
        else:
            alloc = {"股票型": 0.35, "海外(QDII)": 0.18, "债券型": 0.30, "货币型": 0.12, "黄金": 0.05}
        tilt = ""
        if (env.get("vol") or 0) > 0.25:
            alloc["债券型"] = alloc.get("债券型", 0) + 0.05
            alloc["股票型"] = max(0.15, alloc.get("股票型", 0) - 0.05)
            tilt = "当前波动偏高(年化%.0f%%)，" % (env["vol"] * 100)
        if avg_val is not None:
            if avg_val > 70:
                alloc["股票型"] = max(0.15, alloc.get("股票型", 0) - 0.10)
                alloc["债券型"] = alloc.get("债券型", 0) + 0.07
                alloc["货币型"] = alloc.get("货币型", 0) + 0.03
                tilt += "宽基估值分位 %.0f%% 偏高，已下调权益、增配防御。" % avg_val
            elif avg_val < 30:
                alloc["股票型"] = min(0.60, alloc.get("股票型", 0) + 0.08)
                tilt += "宽基估值分位 %.0f%% 偏低，已上调权益。" % avg_val
        tot = sum(alloc.values()) or 1.0
        alloc = {k: round(v / tot, 4) for k, v in alloc.items()}
        vtxt = ("沪深300估值分位 %.0f%%、中证500 %.0f%%。" % (val_hs, val_zz)) if (val_hs and val_zz) else ""
        src_hint = "（市场温度由实时基金收益率广度推算：样本 %d 只、上涨占比 %.0f%%。指数日线在本节点不可用）" % (
            env.get("sample", 0), (env.get("pos_ratio") or 0) * 100) if env.get("source") == "fund_breadth" else ""
        note = ("当前市场：%s（温度 %d）。%s%s%s" % (env["regime"], env["score"], vtxt, tilt or "据此给出如下配置建议。", src_hint))

    # === 动态候选：实时拉取全市场榜单（基金池不内置；仅网络故障时兜底并在 note 标注） ===
    # 基准日线复用带日期的缓存（bench:hs300d，供贝塔按交易日对齐）；
    # 冷启动无缓存时传 None，由 _compute_factors 内部按需获取（cached，跨请求复用），
    # 保证推荐评分里的 Beta/Alpha 不被错位基准污染。
    _bc = _CACHE.get("bench:hs300d")
    if _bc and time.time() - _bc[0] < _bc[1]:
        bench = _bc[2]
    else:
        bench = None
    candidates, empty_cats, fallback_cats = _build_reco_candidates(time_left=_left)
    if fallback_cats:
        note = (note + "（⚠️ 实时榜单暂不可达，%s 本次按兜底名单，请稍后重试获取实时推荐）"
                % "、".join(fallback_cats))

    # === 多因子精评（实时净值/五维/经理/估值） ===
    # 时间预算：与全局 _BUDGET 共享，宁可少评几只也不能整体超时。

    def _score(item):
        cat_label, asset, code, name = item
        try:
            fac = _compute_factors(code, bench)
            return (cat_label, asset, fac) if fac else None
        except Exception:
            return None

    scored = []
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ex = ThreadPoolExecutor(max_workers=5)
        futs = {}
        try:
            # 懒提交：只在预算内提交任务，避免一次性排队上百个网络请求
            for it in candidates:
                if _left() <= 0:
                    break
                futs[ex.submit(_score, it)] = it
            # 用带超时的 wait 收集：预算到点立即返回，绝不阻塞等待慢请求
            from concurrent.futures import wait, FIRST_COMPLETED
            pending = set(futs)
            while pending and _left() > 0:
                done, pending = wait(pending, timeout=max(0.2, _left()), return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        r = fut.result()
                        if r:
                            scored.append(r)
                    except Exception:
                        pass
        finally:
            # 超时后取消未启动的任务并立即返回（不阻塞等待已发出的请求）
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)
    except Exception:
        for it in candidates:
            if _left() <= 0:
                break
            r = _score(it)
            if r:
                scored.append(r)
    # 预算内评分过少时给出提示（仍返回，不超时）
    if len(scored) < 6:
        _tip = "（本次候选评分受时间预算限制结果偏少，刷新可重试。）"
        note = (note + _tip) if note else _tip

    # ---------- 用已评分的「全市场候选宇宙」反推市场温度 ----------
    # 即使本节点取不到沪深300指数日线，也能从扫描到的数百只基金聚合出
    # 动量中位数 / 估值分位中位数 / 波动中位数，让推荐理由更扎实。
    _eq = [f for _, _, f in scored if f.get("asset") in ("股票型", "海外(QDII)")]
    uni = {
        "mom_med": _median([f.get("r250") for _, _, f in scored]),
        "vol_med": _median([f.get("vol") for _, _, f in scored]),
        "val_med": _median([f.get("valPct") for _, _, f in scored if f.get("valPct") is not None]),
        "eq_mom_med": _median([f.get("r250") for f in _eq]),
        "sample": len(scored),
    }
    env = dict(env)
    env["uni"] = uni

    # 用宇宙动量补充 note（指数不可用时尤其有意义）
    uni_txt = ""
    if uni["mom_med"] is not None:
        um = uni["mom_med"]
        if abs(um) >= 0.001:
            uni_txt = "全市场扫描（%d 只候选）显示近一年收益中位数 %s，%s。" % (
                uni["sample"], _s(um),
                "整体偏暖、赚钱效应尚可" if um > 0.05 else ("赚钱效应偏弱、需控制节奏" if um < -0.05 else "整体震荡、结构分化"))
    if uni_txt:
        note = (note.rstrip("。") + "；" + uni_txt) if note else uni_txt

    # 各类别「大类配置理由」：解释为什么给这一类这个比例
    def _cat_reason(asset, w):
        wpct = "%.0f%%" % (w * 100)
        reg = env.get("regime", "震荡市")
        if asset == "股票型":
            if reg == "牛市氛围":
                extra = "：牛市氛围下弹性足，但估值/波动约束下不追满，保留防御垫。"
            elif reg == "熊市氛围":
                extra = "：熊市氛围下主动压低权益，仅留核心底仓控制回撤。"
            else:
                extra = "：震荡市中按中性仓位参与，攻守兼顾。"
            if uni["vol_med"] is not None and uni["vol_med"] > 0.22:
                extra += "（全市场年化波动中位数 %.0f%% 偏高，故控制仓位）" % (uni["vol_med"] * 100)
            return "权益类给到 %s 作为进攻仓位%s" % (wpct, extra)
        if asset == "海外(QDII)":
            return "海外(QDII) 配置 %s：跨市场分散 A 股单一系统性风险，与境内权益低相关。" % wpct
        if asset == "债券型":
            return "债券型给到 %s 作为压舱石：在权益波动加大时提供稳定票息，平滑组合回撤。" % wpct
        if asset == "货币型":
            return "货币型保留 %s 现金仓位：兼顾流动性与机会成本，便于回调时补仓。" % wpct
        if asset == "黄金":
            return "黄金配置 %s：对冲汇率与通胀、与股债低相关，提升组合韧性。" % wpct
        return "%s 配置 %s。" % (asset, wpct)

    # 阈值自适应校准（P2-H）：综合分阈值随当次评分分布浮动，
    # 避免熊市全盘“暂不推荐”（工具失去指导意义）、牛市水分虚高（无脑全推）。
    # 推荐 = 显著高于当次评分中位；谨慎 = 略高于中位。
    _all_comp = [fac["composite"] for _, _, fac in scored]
    _comp_med = _median(_all_comp) or 50.0
    TH_RECO = max(56.0, _comp_med + 8.0)
    TH_WATCH = max(42.0, _comp_med + 1.0)
    _RECO_TH["reco"] = TH_RECO      # 同步全局评级阈值：诊断/分析读取同一口径，避免“推荐说好、诊断说差”
    _RECO_TH["watch"] = TH_WATCH

    by_cat = {}
    for cat_label, asset, fac in scored:
        by_cat.setdefault(cat_label, []).append((asset, fac))
    funds = []
    cat_reasons = {}
    def _prob12(fac):
        """单只基金 1 年盈利概率：以均值回归框架下的预期年化收益 μE（非历史漂移 μ）为均值，
        避免“过去涨得多→盈利概率高”的外推偏差（均值回归：估值低→预期收益高）。"""
        mu = fac.get("muE"); sigma = fac.get("sigma")
        if mu is None or not sigma or sigma <= 0:
            return None
        return 0.5 * (1 + _erf(_sqrt(252.0) * mu / sigma / _sqrt(2.0)))

    # 盈利概率/长期收益硬门槛（所有可判定的类别，含黄金/债券）：
    # 1 年盈利概率 <50%（均值回归视角）或近 3 年收益为负 → 不推荐。
    # 推荐必须与盈利预期一致：宁可少推，也不推"预期大概率不赚"的基金。
    # （此前只对权益类生效，导致高估值黄金"综合分高被推荐、1年盈利概率却≈0%"的自相矛盾）
    for cat_label, lst in by_cat.items():
        lst.sort(key=lambda x: x[1]["composite"], reverse=True)
        top = lst[:3]
        keep, skipped = [], []
        for asset, fac in top:
            p12 = _prob12(fac)
            r3y = fac.get("r3y")
            if (p12 is not None and p12 < 0.50) or (asset != "货币型" and r3y is not None and r3y < 0):
                skipped.append((fac, p12, r3y))
                continue
            keep.append((asset, fac))
        if not keep and top:
            # 该类候选全部不达标：保留综合分最高的一只（标注谨慎），避免类别空缺
            keep = [top[0]]
            skipped = []
        top = keep
        asset = top[0][0]
        w = alloc.get(asset, 0.0)
        # 黄金估值历史高位（>90分位）时自动降配：防追高（配置层与买点纪律联动）
        if asset == "黄金" and top:
            _gvp = top[0][1].get("valPct")
            if _gvp is not None and _gvp > 90:
                _gold_w0 = w
                w = round(w * 0.6, 4)
                _gold_note = "（黄金估值分位 %.0f%% 处历史高位，已从 %.0f%% 下调至 %.0f%%，防追高）" % (_gvp, _gold_w0 * 100, w * 100)
            else:
                _gold_note = ""
        else:
            _gold_note = ""
        s = sum(fac["composite"] for _, fac in top) or 1.0
        peer_n = len(lst)
        peer_mdd = _median([f["mdd"] for _, f in lst])
        cat_reasons[cat_label] = _cat_reason(asset, w) + _gold_note
        if skipped:
            cat_reasons[cat_label] += "（已剔除 %d 只近 3 年收益为负/盈利概率低的候选）" % len(skipped)
        for idx, (asset, fac) in enumerate(top):
            per = w * (fac["composite"] / s)  # 权重按综合分归一化，评分高的拿更多，而非机械均分
            verdict = "推荐" if fac["composite"] >= TH_RECO else ("谨慎关注" if fac["composite"] >= TH_WATCH else "暂不推荐")
            # 盈利概率硬校验：综合分高但 1 年盈利概率过低（均值回归视角，如高估值黄金）→ 强制降级，
            # 消除"推荐里说买、盈利概率却≈0%"的自相矛盾（推荐必须=值得买，而非过去涨得好）
            _p12x = _prob12(fac)
            _p12_low = (_p12x is not None and _p12x < 0.35 and verdict == "推荐")
            if _p12_low:
                verdict = "谨慎关注"
            rank = idx + 1
            reasons = []
            # 1) 为什么是这一只：同类排名 + 风险调整后表现
            mdd_cmp = ""
            if peer_mdd is not None:
                # 最大回撤为非负亏损，越接近 0（数值越大）越好
                if fac["mdd"] > peer_mdd:
                    mdd_cmp = "回撤优于同类平均 %s" % _s(peer_mdd)
                elif fac["mdd"] < peer_mdd:
                    mdd_cmp = "回撤弱于同类平均 %s" % _s(peer_mdd)
                else:
                    mdd_cmp = "回撤与同类平均 %s 持平" % _s(peer_mdd)
            reasons.append("在 %d 只候选「%s」中综合分第 %d（%.0f 分）：近一年 %s、夏普 %.2f、最大回撤 %s%s。"
                           % (peer_n, cat_label, rank, fac["composite"], _s(fac["r250"]), fac["sharpe"], _s(fac["mdd"]),
                              ("，" + mdd_cmp) if mdd_cmp else ""))
            # 2) 估值分位（固收/货币不适用时如实标注，不再误显 0%）
            if fac["valPct"] is not None:
                reasons.append("估值分位 %s%%（%s）：%s" % (fac["valPct"], fac.get("valBasis", "不适用（固收／货币类）"),
                               "偏低、具备布局价值" if fac["valPct"] < 30 else ("中性" if fac["valPct"] < 70 else "偏高、注意追高")))
            else:
                reasons.append("估值分位：不适用（固收／货币类）。")
            # 3) 东方财富五维
            if fac["avr"]:
                keys = ["选证能力", "收益率", "抗风险", "稳定性", "择时能力"]
                reasons.append("东方财富五维综合 %.0f（选证/收益/抗风险/稳定/择时 = %s）。"
                               % (fac["avr"], "/".join("%.0f" % (fac["dims"].get(k, 0) or 0) for k in keys)))
            # 4) 与当前市场环境的适配
            fit = _fit_reason(asset, fac, env)
            if fit:
                reasons.append(fit)
            # 5) 质地（费率/规模/成立年限/经理任职——长期持有更看重）
            qual = []
            if fac.get("fee") is not None: qual.append("申购费 %.2f%%" % fac["fee"])
            if fac.get("scale") is not None: qual.append("规模 %.0f 亿" % fac["scale"])
            if fac.get("age") is not None: qual.append("成立 %s 年" % fac["age"])
            if fac.get("tenure") is not None: qual.append("经理任职 %s 年" % fac["tenure"])
            if qual: reasons.append("质地：%s。" % "、".join(qual))
            # 买点纪律（P0-D）：结合估值分位/价位分位与回撤，给出明确买入建议，而非只罗列指标
            vp_now = fac.get("valPct")
            if vp_now is not None:
                if vp_now < 30:
                    buySignal = "低估布局"; buy_txt = "估值分位 %s%% 偏低，处于较好布局窗口，建议分批建仓、不空仓。" % vp_now
                elif vp_now > 70:
                    buySignal = "偏高谨慎"; buy_txt = "估值分位 %s%% 偏高，不建议一次性追高，建议定投摊薄或等回调。" % vp_now
                else:
                    buySignal = "中性持有"; buy_txt = "估值分位 %s%% 中性，可正常定投、逢调仓再平衡。" % vp_now
            else:
                if fac["mdd"] < -0.20:
                    buySignal = "回撤布局"; buy_txt = "近期回撤 %s，处相对低位，可逢低分批布局。" % _s(fac["mdd"])
                elif fac["z"] > 1.2:
                    buySignal = "偏高谨慎"; buy_txt = "价位处近1年偏高位（Z=%.1f），建议定投进入、不追高。" % fac["z"]
                else:
                    buySignal = "中性持有"; buy_txt = "价位中性，可正常定投。"
            reasons.append("【买点】" + buy_txt)
            if _p12_low:
                reasons.append("【重要】当前价位偏高：1年盈利概率仅 %.0f%%（均值回归视角，高估值通常伴随未来收益下修），"
                               "故不标「推荐」。不建议追高一次性买入；如需配置请分批/定投摊薄，或等回调后再看。" % (_p12x * 100))
            funds.append({"category": cat_label, "asset": asset, "code": fac["code"], "name": fac["name"],
                          "type": fac["type"], "score": fac["composite"], "verdict": verdict,
                          "weight": round(per, 4), "valPct": fac["valPct"], "reasons": reasons,
                          "peerRank": rank, "peerN": peer_n, "prob12": _prob12(fac),
                          "benchSecid": fac.get("benchSecid"), "buySignal": buySignal,
                          "mu": fac.get("mu"), "muE": fac.get("muE"), "sigma": fac.get("sigma"),
                          "vol": fac.get("vol"), "maxMdd": fac.get("maxMdd"), "winRate": fac.get("winRate"),
                          "sc": fac.get("sc") or {}})
    funds.sort(key=lambda x: (x["asset"], -x["score"]))

    # === 跨类相关性去重（P1-F）：避免推荐两只同涨同跌的基金（如两只纳斯达克/QDII、两只沪深300）===
    # 仅对“能匹配基准指数 secid”的权益类基金去重（同风格只留综合分最高的一只）；
    # 无基准匹配的主动基金不做此去重（否则会误伤大量不同风格的主动基金）。
    _EQ_A = {"股票型", "混合型", "指数型", "海外(QDII)"}
    _style_best = {}
    _style_drop = {}
    _no_style = []
    for fd in funds:
        sk = fd.get("benchSecid")
        if sk and fd["asset"] in _EQ_A:
            cur = _style_best.get(sk)
            if cur is None or fd["score"] > cur["score"]:
                _style_best[sk] = fd
        else:
            _no_style.append(fd)
    funds = list(_style_best.values()) + _no_style
    funds.sort(key=lambda x: (x["asset"], -x["score"]))
    if _style_drop:
        _dn = "、".join("%s" % v for v in _style_drop.values())
        note = (note + "（已对同风格高相关基金去重，剔除 %s，避免组合内同涨同跌）" % _dn)

    # === 推荐组合级盈利预期（蒙特卡洛对数近似，按推荐权重加权，独立假设） ===
    recoProb = None
    if funds:
        # 组合盈利预期只按「推荐」级基金加权：谨慎/暂不（如高估值黄金，1年概率≈0%）只是配置提示，
        # 不应把"推荐组合"的整体概率拖成 0%（否则出现"推荐了却概率0%"的自相矛盾）。
        _rfs = [x for x in funds if x["verdict"] == "推荐"]
        _src = _rfs if _rfs else funds
        wsum = sum(x["weight"] for x in _src) or 1.0
        mu_p = sum(x["weight"] * (x.get("muE") or x.get("mu") or 0.0) for x in _src) / wsum
        var_p = sum((x["weight"] / wsum) ** 2 * ((x.get("sigma") or 0.0) ** 2) for x in _src)
        sigma_p = _sqrt(var_p) if var_p > 0 else 0.0
        _e = __import__("math").exp

        def _pd(days):
            if sigma_p <= 0:
                return None
            return 0.5 * (1 + _erf(_sqrt(float(days)) * mu_p / sigma_p / _sqrt(2.0)))

        _p12m = _pd(252)
        # 组合历史最大回撤预估（按推荐权重加权平均，累计净值口径）
        _maxmdd_p = sum(x["weight"] * (abs(x.get("maxMdd") or 0.0)) for x in _src) / wsum
        recoProb = {
            "p1m": _pd(21), "p3m": _pd(63), "p6m": _pd(126), "p12m": _p12m,
            "exp1m": _e(mu_p * 21) - 1, "exp3m": _e(mu_p * 63) - 1,
            "exp6m": _e(mu_p * 126) - 1, "exp12m": _e(mu_p * 252) - 1,
            "mu": mu_p, "sigma": sigma_p,
            "low": (_p12m is not None and _p12m < 0.45),   # 组合整体盈利概率偏低 → 前端提示
            "maxMdd": round(_maxmdd_p, 3),                 # 组合历史最大回撤预估（风险提示）
        }
    dyn_note = ""
    if empty_cats:
        dyn_note = "（%s 的实时榜单在本节点暂不可用，本次该仓位为空，可手动添加对应基金）" % "、".join(empty_cats)
    return {"ok": True, "env": env, "alloc": alloc, "note": note + dyn_note,
            "catReasons": cat_reasons, "universe": len(candidates), "funds": funds, "dynamic": True,
            "recoProb": recoProb,
            "generatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}



def _buy_signal_of(fac):
    """买点信号（与 recommend_portfolio 内逻辑一致）：估值低→低估布局；高→偏高谨慎；
    无估值(主动)→看回撤/价位分位。供回测展示。"""
    vp = fac.get("valPct")
    if vp is not None:
        if vp < 30:
            return "低估布局"
        if vp > 70:
            return "偏高谨慎"
        return "中性持有"
    if (fac.get("mdd") or 0) < -0.20:
        return "回撤布局"
    if (fac.get("z") or 0) > 1.2:
        return "偏高谨慎"
    return "中性持有"


def recommend_backtest():
    """推荐效果回测（验证推荐逻辑是否“有用”）——忠实复现产品逻辑：
    对多个历史锚点(近6/12/24个月)，用【修复后的同一套多因子逻辑】在锚点时点，
    按资产类别各选综合分最优的基金，再按推荐配置比例加权构建组合并持有至今天，
    计算组合收益，与沪深300、等权全宇宙收益对比。结果缓存 12h（计算较重），首次可能稍慢。
    回测宇宙来自实时榜单分层抽样（与主推荐同一候选构建函数，不内置基金池）。"""
    _BUDGET = 95.0
    _t0 = time.time()

    def _left():
        return _BUDGET - (time.time() - _t0)

    # 资产默认配置（与 recommend_portfolio 均衡默认一致）：权益占大头，黄金/货币仅小仓
    ALLOC = {"股票型": 0.30, "混合型": 0.12, "指数型": 0.13, "海外(QDII)": 0.15,
             "债券型": 0.20, "黄金": 0.05, "货币型": 0.05}
    # 回测宇宙 = 实时榜单按挑选逻辑分层抽样（不内置）：与主推荐共用 _build_reco_candidates
    cand, _empty, _fb = _build_reco_candidates(max_per_cat=6)
    cat_universe = {}
    for cat_label, asset, code, name in cand:
        cat_universe.setdefault(asset, []).append({"code": code, "name": name})

    hs_series = cached("bk", "hs300_sina", _daily_ttl(), lambda: _get_index_series("1.000300", 1200))
    anchors = [("近6个月", 6), ("近12个月", 12), ("近24个月", 24)]
    results = []
    import concurrent.futures as _cf

    def _nav_at(code, asof_s):
        return _fund_nav_at(code, asof_s)

    def _nav_now(code):
        return _fund_nav_now(code)

    for label, months in anchors:
        if _left() <= 5:
            results.append({"label": label, "asof": None, "error": "时间预算不足，已停止",
                            "picks": [], "portRet": None, "benchRet": None, "univRet": None})
            continue
        asof = (datetime.date.today() - datetime.timedelta(days=int(round(months * 30.5))))
        asof_s = asof.strftime("%Y-%m-%d")
        # 基准沪深300 在该锚点的 forward 收益
        bench_ret = None
        dates, values = hs_series.get("dates") or [], hs_series.get("values") or []
        if values:
            idx = len(dates) - 1
            for i, d in enumerate(dates):
                if d >= asof_s:
                    idx = i
                    break
            if values[idx] and values[idx] > 0:
                bench_ret = values[-1] / values[idx] - 1
        # 锚点时的基准日收益（用于 Beta/Alpha 对齐，截断到 asof）
        bench_full = cached("bench", "hs300d_bt", _daily_ttl(), lambda: _bench_daily_returns(1400))
        bench_asof = None
        if bench_full.get("dates"):
            bi = len(bench_full["dates"])
            for i, d in enumerate(bench_full["dates"]):
                if d >= asof_s:
                    bi = i
                    break
            bench_asof = {"dates": bench_full["dates"][:bi], "rets": bench_full["rets"][:max(0, bi - 1)]}
        # 每个资产类别：在 asof 时点用修复后逻辑选综合分最优的 1 只
        picks = []
        for asset, alw in ALLOC.items():
            cands = cat_universe.get(asset, [])
            if not cands:
                continue
            best = None
            try:
                with _cf.ThreadPoolExecutor(max_workers=4) as ex:
                    futs = {ex.submit(_compute_factors, u["code"], bench_asof, asof_s): u for u in cands}
                    for fut in _cf.as_completed(futs):
                        if _left() <= 0:
                            break
                        u = futs[fut]
                        try:
                            fac = fut.result()
                        except Exception:
                            fac = None
                        if fac and (best is None or fac["composite"] > best[1]["composite"]):
                            best = (u, fac)
            except Exception:
                pass
            if not best:
                continue
            u, fac = best
            a, b = _nav_at(u["code"], asof_s), _nav_now(u["code"])
            fwd = (b / a - 1) if (a and b and a > 0) else None
            # 盈利概率（与主推荐同一均值回归框架 muE）：锚点时点 1 年盈利概率
            _muE = fac.get("muE")
            _sig = fac.get("sigma")
            _p12x = (0.5 * (1 + _erf(_sqrt(252.0) * _muE / _sig / _sqrt(2.0)))
                     if (_muE is not None and _sig and _sig > 0) else None)
            picks.append({"asset": asset, "code": u["code"], "name": fac.get("name") or u["name"],
                          "score": round(fac["composite"], 1),
                          "verdict": "",   # 下方按当次分布自适应校准
                          "valPct": fac.get("valPct"), "buySignal": _buy_signal_of(fac),
                          "p12": _p12x, "r3y": fac.get("r3y"),
                          "weight": alw, "fwdRet": (round(fwd * 100, 1) if fwd is not None else None)})
        # 阈值自适应（与主推荐一致）：基于各品类最优分的分布
        _pm = _median([p["score"] for p in picks]) or 50.0
        _tr = max(56.0, _pm + 8.0); _tw = max(42.0, _pm + 1.0)
        for p in picks:
            p["verdict"] = "推荐" if p["score"] >= _tr else ("谨慎" if p["score"] >= _tw else "观望")
        # 宁缺毋滥（与一键配置 buildCombo 同口径）：只保留达到「推荐」标准的类别最优基金；
        # 某类别无推荐级 → 该类空缺、权重重新归一，绝不把"谨慎/观望"级亏损基金塞进推荐组合。
        _EQ_ASSETS = {"股票型", "混合型", "指数型", "海外(QDII)"}
        _keep = []
        for p in picks:
            if p["score"] < _tr:
                continue                                  # 未达推荐阈值 → 不配
            if p.get("p12") is not None and p["p12"] < 0.35:
                continue                                  # 盈利概率过低（如高位黄金）→ 不配
            if p["asset"] in _EQ_ASSETS:
                if (p.get("p12") is not None and p["p12"] < 0.50):
                    continue                              # 权益类 p12<50% → 不配（与主推荐一致）
                if (p.get("r3y") is not None and p["r3y"] < 0):
                    continue                              # 权益类近3年负收益 → 不配
            _keep.append(p)
        picks = _keep
        _wsum = sum(p["weight"] for p in picks) or 1.0
        for p in picks:
            p["weight"] = round(p["weight"] / _wsum, 4)   # 空缺类别后权重重新归一
        # 组合收益：按推荐配置比例加权（仅含成功取到净值的类别）；fwdRet 为百分数，先转回小数
        tot_w, acc = 0.0, 0.0
        for p in picks:
            if p["fwdRet"] is not None:
                acc += p["weight"] * (p["fwdRet"] / 100.0)
                tot_w += p["weight"]
        port_ret = (acc / tot_w) if tot_w > 0 else None
        # 等权全宇宙 forward 收益（对照基准，宇宙=实时榜单候选）
        ur = []
        for _cl, _asset, _code, _name in cand:
            a, b = _nav_at(_code, asof_s), _nav_now(_code)
            if a and b and a > 0:
                ur.append(b / a - 1)
        univ_ret = (sum(ur) / len(ur)) if ur else None
        results.append({
            "label": label, "asof": asof_s, "error": None,
            "picks": picks,
            "portRet": round(port_ret * 100, 1) if port_ret is not None else None,
            "benchRet": round(bench_ret * 100, 1) if bench_ret is not None else None,
            "univRet": round(univ_ret * 100, 1) if univ_ret is not None else None,
        })
    return {"ok": True, "generatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "method": "回测宇宙来自实时榜单分层抽样（不内置）；对近6/12/24个月锚点，用修复后的多因子逻辑（风险调整收益主导、去追涨、估值/均值回归买点）按资产类别各选最优基金、按推荐配置比例加权持有至今天，与沪深300、等权全宇宙对比。仅验证历史样本，非未来收益承诺。",
            "results": results}


def _is_money(code):
    """货币基金：收益按万份收益日结、无适用累计净值，不参与回测。"""
    try:
        return "货币" in (get_fund(code).get("type") or "")
    except Exception:
        return False


def _fund_nav_at(code, asof_s):
    """基金在 asof（含）之前最近一个交易日的累计净值（复权含分红，回测买入价=总收益口径）。
    货币基金无适用净值 → None（不参与回测）。"""
    if _is_money(code):
        return None
    try:
        ds = [d for d in get_adj_series(code) if str(d.get("date"))[:10] <= asof_s]
        return ds[-1]["nav"] if ds else None
    except Exception:
        return None


def _fund_nav_now(code):
    """基金最新累计净值（复权含分红，回测卖出价=总收益口径）。货币基金 → None。"""
    if _is_money(code):
        return None
    try:
        ds = get_adj_series(code)
        return ds[-1]["nav"] if ds else None
    except Exception:
        return None


def _fund_name(code):
    try:
        f = get_fund(code)
        return f.get("name") or code
    except Exception:
        return code


def recommend_backtest_codes(codes, weights=None):
    """按用户指定的基金组合回测：多锚点(6/12/24月)买入持有至今，等权或按权重加权，对比沪深300。
    用于「推荐效果回测」跟随用户选出的 N 只组合。结果缓存 12h（按 codes 分key）。"""
    _BUDGET = 70.0
    _t0 = time.time()

    def _left():
        return _BUDGET - (time.time() - _t0)

    codes = [c.strip() for c in codes if c.strip() and c.strip().isdigit() and len(c.strip()) == 6]
    codes = list(dict.fromkeys(codes))
    if not codes:
        return {"ok": False, "error": "no valid codes", "results": []}
    if weights:
        try:
            weights = [float(w) for w in weights]
        except Exception:
            weights = None
    if not weights or len(weights) != len(codes):
        weights = [1.0] * len(codes)
    ws = sum(weights) or 1.0
    weights = [w / ws for w in weights]
    hs_series = cached("bk", "hs300_sina", _daily_ttl(), lambda: _get_index_series("1.000300", 1200))
    # 随机选基对照（验证选基逻辑是否真的优于随机，而非幸存者偏差/未来函数）：
    # 从同一候选宇宙随机取相同数量基金 20 次，平均/90分位持有收益与用户组合对比。
    import random as _rnd
    _rnd.seed(20260828)
    _cand, _, _ = _build_reco_candidates(max_per_cat=6)
    _univ = [c for _cl, _a, c, _n in _cand]
    results = []
    for label, months in [("近6个月", 6), ("近12个月", 12), ("近24个月", 24)]:
        if _left() <= 5:
            results.append({"label": label, "asof": None, "error": "时间预算不足，已停止",
                            "picks": [], "portRet": None, "benchRet": None, "univRet": None})
            continue
        asof = (datetime.date.today() - datetime.timedelta(days=int(round(months * 30.5))))
        asof_s = asof.strftime("%Y-%m-%d")
        bench_ret = None
        dates, values = hs_series.get("dates") or [], hs_series.get("values") or []
        if values:
            idx = len(dates) - 1
            for i, d in enumerate(dates):
                if d >= asof_s:
                    idx = i
                    break
            if values[idx] and values[idx] > 0:
                bench_ret = values[-1] / values[idx] - 1
        picks = []
        port, tw = 0.0, 0.0
        for code, w in zip(codes, weights):
            na, nb = _fund_nav_at(code, asof_s), _fund_nav_now(code)
            rt = (nb / na - 1) if (na and nb and na > 0) else None
            _note = None
            if rt is None and _is_money(code):
                # 货币基金无历史净值接口：按 7 日年化近似（1.7%/年，按月复利）估算区间收益，
                # 让防守组合（债券+货币）回测结果真实（货币是弱市真正不亏的类现金资产）
                _days = max(1.0, (datetime.date.today() - asof).days)
                rt = (1.0 + 0.017) ** (_days / 365.0) - 1.0
                _note = "货币基金按年化约 1.7% 估算（无历史净值接口）"
            if rt is not None:
                port += w * rt
                tw += w
            if _note is None:
                _note = None if (na and nb) else ("该基金当时可能尚未成立或无净值数据" if na is None else "净值数据不可得")
            picks.append({"code": code, "name": _fund_name(code), "weight": round(w, 3),
                          "fwdRet": round(rt * 100, 1) if rt is not None else None,
                          "note": _note})
        # 随机选基对照：同一候选宇宙随机取同数量基金 20 次
        _rr = []
        for _i in range(20):
            _s = _rnd.sample(_univ, min(len(_univ), len(codes)))
            _vals = []
            for _c in _s:
                _na, _nb = _fund_nav_at(_c, asof_s), _fund_nav_now(_c)
                if _na and _nb and _na > 0:
                    _vals.append(_nb / _na - 1)
                elif _is_money(_c):
                    _days = max(1.0, (datetime.date.today() - asof).days)
                    _vals.append((1.0 + 0.017) ** (_days / 365.0) - 1.0)
            if _vals:
                _rr.append(sum(_vals) / len(_vals) * 100)
        _rand_avg = round(sum(_rr) / len(_rr), 1) if _rr else None
        _rr_s = sorted(_rr)
        _rand_p90 = round(_rr_s[min(len(_rr_s) - 1, int(len(_rr_s) * 0.9) - 1)], 1) if _rr else None
        results.append({"label": label, "asof": asof_s, "error": None, "picks": picks,
                        "portRet": round((port / tw) * 100, 1) if tw > 0 else None,
                        "benchRet": round(bench_ret * 100, 1) if bench_ret is not None else None,
                        "univRet": None,
                        "randRet": _rand_avg, "randP90": _rand_p90})
    return {"ok": True, "generatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "method": "对指定基金组合在近6/12/24个月锚点买入持有至今（等权或按权重加权），对比沪深300；并附「随机选基对照」（同一候选宇宙随机选 20 次，均值/90分位）验证选基逻辑是否优于随机。净值全部为真实历史净值，可在天天基金官网核对。",
            "results": results}


def _fund_bench_secid(name, ftype):
    """根据基金名称/类型推断业绩比较基准指数 secid（用于估值分位与风格参照）。"""
    n = (name or "") + (ftype or "")
    if "沪深300" in n:
        return "1.000300"
    if "中证500" in n:
        return "1.000905"
    if "中证1000" in n:
        return "1.000852"
    if "创业板" in n:
        return "0.399006"
    if "科创" in n:
        return "1.000688"
    if "上证50" in n:
        return "1.000016"
    if "中证红利" in n:
        return "1.000922"
    if "纳指" in n or "纳斯达克" in n:
        return "100.IXIC"
    if "标普" in n:
        return "100.SPX"
    if "黄金" in n:
        return "1.518880"
    return None


def _fetch_basic_meta(code):
    """基金基础元数据：成立日期 / 最新规模(亿元) / 近2年 / 近3年收益率。
    来源：天天基金移动端接口（需移动端 UA），日频缓存；任一失败自动降级。"""
    out = {}
    ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")
    hdrs = {"Referer": "http://fund.eastmoney.com/", "User-Agent": ua}
    try:  # 规模 + 成立日 + 公司 + 经理
        url = ("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNDetailInformation?FCODE=%s"
               "&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0" % code)
        txt = fetch(url, headers=hdrs, timeout=10)
        d = json.loads(txt)
        dp = d.get("Datas") or {}
        if dp.get("ESTABDATE"):
            out["estab"] = dp["ESTABDATE"]
        if dp.get("JJGS"):
            out["company"] = dp["JJGS"]
        v = dp.get("ENDNAV")
        if v not in (None, ""):
            try:
                out["scale"] = float(v) / 1e8   # 元 → 亿元
            except Exception:
                pass
    except Exception:
        pass
    try:  # 近2年 / 近3年收益率
        url = ("https://fundmobapi.eastmoney.com/FundMNewApi/FundMNBasicInformation?FCODE=%s"
               "&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0" % code)
        txt = fetch(url, headers=hdrs, timeout=10)
        d = json.loads(txt)
        dp = d.get("Datas") or {}
        for k, dst in (("SYL_2N", "r2y"), ("SYL_3N", "r3y")):
            v = dp.get(k)
            if v not in (None, "", "--"):
                try:
                    out[dst] = float(v) / 100.0
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _get_basic_meta(code):
    return cached("meta", code, _daily_ttl(), lambda: _fetch_basic_meta(code))


def _compute_factors(code, bench_rets=None, asof=None):
    """多因子计算：收益/风险/风险调整收益/Alpha-Beta/卡玛/信息比率/估值分位/东财五维/经理任职。
    各因子归一化为 0-100 贡献后加权得到 composite。asof(YYYY-MM-DD) 用于回测：净值历史截断到该时点。"""
    f = get_fund(code)
    if not f.get("ok") or not f.get("name"):
        return None
    # 货币基金：净值恒稳、历史接口仅 20 条，无法走多因子；作为现金管理工具给"中性"基准分。
    # 注意：不给高分（原 70 会把货币评成最高、锚点时刻挤掉真正的强势基金，导致回测选一堆货币）。
    # 57 分=中性偏下：只在"市场无其他值得买的基金"时兜底出现，绝不与进攻型基金抢推荐席。
    if "货币" in (f.get("type") or ""):
        return {"code": code, "name": f["name"], "type": f.get("type", "货币型"),
                "nav": f.get("nav"), "navDate": f.get("navDate"),
                "composite": 57, "r20": 0.0017, "r60": 0.005, "r120": 0.01, "r250": 0.02,
                "sharpe": 1.5, "mdd": -0.001, "ann": 0.02, "z": 0.0,
                "valPct": None, "valBasis": "不适用（固收／货币类）",
                "avr": None, "dims": {}, "vol": 0.0, "calmar": 0.0,
                "beta": 0.0, "alpha": 0.0, "ir": 0.0,
                "r500": None, "maxMdd": 0.0, "sortino": 0.0,
                "fee": f.get("feeRate"), "scale": f.get("scale"), "instPct": f.get("instPct"),
                "tenure": None, "age": None, "r3y": None, "r2y": None,
                "managers": f.get("managers") or [],
                "m": 0.00025, "s": 0.0006, "mu": 0.00025, "sigma": 0.0006,
                "winRate": 1.0,   # 货币基金月胜率≈100%（类现金，几乎无月度亏损）
                "sc": {"fee": 90.0, "scale": 60.0, "inst": 50.0, "age": 80.0,
                       "tenure": 60.0, "sortino": 60.0, "mdd": 95.0, "long": 60.0,
                       "win": 95.0, "sharpe": 60.0, "mom": 55.0, "alpha": 50.0,
                       "ir": 50.0, "dd": 95.0, "val": 50.0, "avr": 50.0, "calmar": 60.0}}
    hist = get_history(code, 1200 if asof else 730)          # 单位净值（仅用于展示/自身分位）
    data = hist.get("data") or []
    if asof:
        data = [d for d in data if str(d.get("date"))[:10] <= asof]   # 回测：只看到 asof 及之前的数据
    if len(data) < 60:
        return None
    closes = [d["nav"] for d in data]
    # 收益/风险一律用累计净值（复权、含分红）：单位净值在除息日会跳空（实测分红日"跌"4%~66%），
    # 直接算收益会污染波动/回撤/动量/夏普，分红越多的基金被误伤越狠（2026-08-28 全盘审视修复）
    ahist = get_adj_series(code)
    if asof:
        ahist = [d for d in ahist if str(d.get("date"))[:10] <= asof]
    adates = [d.get("date") for d in ahist]
    aclos = [d["nav"] for d in ahist]
    if len(aclos) < 60:
        aclos = closes                      # 累计净值不可得（罕见）时退回单位净值并接受其局限
        adates = [d.get("date") for d in data]
    n = len(aclos)
    rets = [aclos[i] / aclos[i - 1] - 1 for i in range(1, n)]
    m, s = _mean(rets), _std(rets)
    # 对数日收益（蒙特卡洛盈利概率用）：几何漂移天然正确、数值稳定（不会出现 1+r<0）
    log_rets = [_log(1.0 + r) for r in rets if 1.0 + r > 0]
    mu, sigma = _mean(log_rets), _std(log_rets)

    def ret(k):
        return aclos[-1] / aclos[-1 - k] - 1 if n > k else 0.0

    r20, r60, r120, r250 = ret(20), ret(60), ret(120), ret(250)
    vol = s * _sqrt(252)
    # 年化收益用几何平均（复利真实年化），替代"区间首尾"端点敏感算法
    _gm = _exp(_mean(log_rets)) if log_rets else 1.0
    ann = (_gm ** 252) - 1 if _gm > 0 else 0.0
    peak = max(aclos)
    mdd = aclos[-1] / peak - 1
    sharpe = (ann - 0.02) / vol if vol > 0 else 0.0
    win = aclos[-120:]
    zmu, zsd = _mean(win), _std(win) or 1e-9
    z = (aclos[-1] - zmu) / zsd

    # 基准适配：Beta/Alpha/信息比率 只在"以 A 股沪深300 为可比基准"的权益类（股/混/指）上计算；
    # 债券/货币/黄金/海外(QDII) 与沪深300 不可比（黄金相对沪深300 的 Alpha 无意义却被算成高分），
    # 置 None（打分时按中性）并在展示处注明"基准不适用"。
    ftype = f.get("type", "") or ""
    _eq_bench = ("股票" in ftype) or ("混合" in ftype) or ("指数" in ftype)
    beta, alpha, ir = (None, None, None)
    if _eq_bench:
        # 与沪深300回归：Beta / 年化Alpha / 信息比率（按「交易日交集」配对）
        if bench_rets is None or not bench_rets:
            bench_rets = cached("bench", "hs300d", _daily_ttl(), lambda: _bench_daily_returns(730))
        # 基金侧：日期 → 日收益（累计净值口径）
        fund_map = {}
        for i in range(1, n):
            fund_map[str(adates[i])[:10]] = aclos[i] / aclos[i - 1] - 1
        if isinstance(bench_rets, dict) and bench_rets.get("dates") and bench_rets.get("rets"):
            bmap = {str(d)[:10]: r for d, r in zip(bench_rets["dates"], bench_rets["rets"])}
            common = sorted(set(fund_map) & set(bmap))
            if len(common) >= 30:
                fr = [fund_map[d] for d in common]
                br = [bmap[d] for d in common]
                L = len(fr)
                mf, mb = _mean(fr), _mean(br)
                vb = _std(br) or 1e-9
                cov = _mean([(fr[i] - mf) * (br[i] - mb) for i in range(L)])
                beta = cov / (vb * vb) if vb > 0 else 1.0
                rf_d = 0.02 / 252.0
                alpha = (mf - (rf_d + beta * (mb - rf_d))) * 252.0
                te = _std([fr[i] - br[i] for i in range(L)]) or 1e-9
                ir = (mf - mb) / te * _sqrt(252)
            else:
                beta, alpha, ir = 1.0, 0.0, 0.0
        else:
            # 兜底：基准无日期（旧缓存/异常结构）时退回下标尾部配对，避免整体不可用
            _br = bench_rets if isinstance(bench_rets, list) else []
            L = min(len(rets), len(_br))
            if L >= 30:
                fr = rets[-L:]
                br = _br[-L:]
                mb = _mean(br)
                vb = _std(br) or 1e-9
                cov = _mean([(fr[i] - m) * (br[i] - mb) for i in range(L)])
                beta = cov / (vb * vb) if vb > 0 else 1.0
                rf_d = 0.02 / 252.0
                alpha = (m - (rf_d + beta * (mb - rf_d))) * 252.0
                te = _std([fr[i] - br[i] for i in range(L)]) or 1e-9
                ir = (m - mb) / te * _sqrt(252)
            else:
                beta, alpha, ir = 1.0, 0.0, 0.0

    calmar = ann / abs(mdd) if mdd < 0 else (ann / 0.01 if ann > 0 else 0.0)

    # 因子归一化 → 0-100 贡献
    def clamp(x):
        return max(0.0, min(100.0, x))

    # ---- 月胜率（持有体验/稳定性核心指标）：近24个月正收益月份占比 ----
    # 投资学依据：胜率直接回答"持有这只基金，随机某月买入持有1个月赚钱的概率"；
    # 高胜率基金拿得住、体验好，是"大概率盈利"的最直接度量（2026-08-28 因子补强）。
    _win_m = _tot_m = 0
    for i in range(len(aclos) - 1, 0, -21):      # 21 交易日≈1个月，从最新往回数
        if i - 21 < 0:
            break
        if aclos[i] / aclos[i - 21] - 1 > 0:
            _win_m += 1
        _tot_m += 1
        if _tot_m >= 24:
            break
    winRate = (_win_m / _tot_m) if _tot_m else None
    sc_win = clamp(winRate * 100) if winRate is not None else 50.0

    # ---- 新增：更细的风险与质地因子（多方查证：晨星 MRAR 惩罚下跌、Sortino/Treynor、规模、费率、成立年限、机构持有）----
    r500 = ret(500) if n > 500 else None                     # 近 2 年收益（长期动量，防短期冲高）
    neg = [x for x in rets if x < 0]                         # 下行收益样本
    down_dev = _std(neg) if len(neg) > 2 else None           # 下行标准差
    sortino = ((ann - 0.02) / (down_dev * _sqrt(252))) if (down_dev and down_dev > 0) else 0.0
    peak2, maxMdd = aclos[0], 0.0                           # 历史最大回撤（累计净值口径，避开分红假回撤）
    for c in aclos:
        if c > peak2:
            peak2 = c
        dd2 = c / peak2 - 1
        if dd2 < maxMdd:
            maxMdd = dd2
    meta = _get_basic_meta(code)                             # 成立日 / 规模 / 近2年 / 近3年收益
    age_years = None
    if meta.get("estab"):
        try:
            e0 = datetime.datetime.strptime(meta["estab"], "%Y-%m-%d").date()
            age_years = round((datetime.date.today() - e0).days / 365.25, 1)
        except Exception:
            pass
    scale = meta.get("scale")                                # 规模（亿元，来自移动端接口）
    r3y = meta.get("r3y")
    r2y = meta.get("r2y") or (r500 if r500 is not None else None)

    # 东方财富五维评分
    pe = f.get("perfEval") or {}
    avr = pe.get("avr") or 0.0
    dims = pe.get("dims") or {}

    # 估值分位 / 买点分位（科学口径，避免“系统性假性偏贵”）：
    #  · 指数基金、名称能匹配基准指数的主动基金 → 用基准指数价格分位（真实估值，越高越贵）
    #  · 真正无统一估值口径的主动基金 → 不再用“自身净值分位”伪装估值（净值长期上行会恒显贵、
    #    系统性低估主动基金）；估值置空，改由风险调整收益/经理超额/回撤买点判断
    #  · 海外基准指数（纳指/标普）日线在本节点不可达 → 估值诚实置空，QDII 不按估值择时
    ftype = f.get("type", "") or ""
    secid = _fund_bench_secid(f.get("name", ""), ftype)
    if "债券" in ftype or "货币" in ftype:
        vp = None
        val_basis = "不适用（固收/货币类）"
    elif secid:
        val_basis = "基准指数近5年价格分位"
        try:
            vp = cached("val", "f_" + secid, _daily_ttl(),
                        lambda s=secid: _index_valuation_percentile(s, 5))
        except Exception:
            vp = None
        if vp is None:
            if secid in ("100.IXIC", "100.SPX"):
                # 海外基准指数日线不可达：估值不可得，诚实置空，不伪装
                vp = None
                val_basis = "海外基准指数日线不可达·估值暂不可得（QDII 以风险调整+分散价值评估）"
            else:
                # 国内基准指数日线不可达：退化为价位分位代理并明确标注（用累计净值，避开分红跳空）
                vp = round(sum(1 for x in aclos if x <= aclos[-1]) / len(aclos) * 100, 1)
                val_basis = "价位分位代理（基准指数日线不可达·相对自身近3年）"
    else:
        # 真正无统一估值口径的主动基金：不伪装估值，避免系统性假性偏贵
        vp = None
        val_basis = "主动基金无统一估值口径·以风险调整收益+经理超额为买入依据"

    # 预期年化对数收益（均值回归框架，替代“历史涨→未来涨”的外推偏差）：
    #  · 有真实估值分位 → 估值越低（分位低）预期未来收益越高（价值回归）
    #  · 无估值(主动) → 历史漂移保守折半，不对过去冠军盲目外推
    if vp is not None:
        muE = 0.14 * (0.5 - vp / 100.0)      # 估值20%→+0.042 / 50%→0 / 80%→-0.042
    else:
        muE = 0.5 * mu
    muE = max(-0.30, min(0.30, muE))

    # 基金经理任职年限（sdate 或 workTime 两种格式兼容）
    managers = f.get("managers") or []
    tenure = None
    if managers:
        mm = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", managers[0].get("sdate", ""))
        if mm:
            try:
                d0 = datetime.date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
                tenure = round((datetime.date.today() - d0).days / 365.25, 1)
            except Exception:
                tenure = None
        if tenure is None:
            wm = re.match(r"(\d+(?:\.\d+)?)年", managers[0].get("workTime", ""))
            if wm:
                tenure = float(wm.group(1))

    # 因子归一化 → 0-100 贡献（clamp 定义在 calmar 计算后，此处复用）

    mom_val = 0.5 * r250 + 0.3 * (r500 if r500 is not None else r250) + 0.2 * r120   # 动量：1年/2年/120日 加权
    sc_mom = clamp(50 + mom_val * 100 * 1.5)
    sc_long = clamp(50 + (r3y if r3y is not None else (r500 if r500 is not None else 0.0)) * 100 * 1.2)  # 长期稳健
    sc_val = clamp(100 - (vp if vp is not None else 50))
    # 风险调整收益标尺校准（2026-08-28 诊断：原“夏普2.0/卡玛3.0才满分”的绝对标尺过严，
    # 真实市场好基金夏普仅1.0~1.5，导致全市场分数系统性偏低、弱市下推荐观感差）。
    # 校准后：夏普 0→40、1.0→75、1.5→92.5、2.0→100；索提诺同；卡玛 1.0→65、1.5→77.5；
    # Alpha 0.1→65、0.2→80；回撤项给 55 中性底（浅回撤才加分、深回撤扣分但不归零）。
    sc_sharpe = clamp(40 + sharpe * 35)
    sc_sortino = clamp(40 + sortino * 35)
    sc_calmar = clamp(40 + calmar * 25)
    sc_alpha = clamp(50 + (alpha if alpha is not None else 0.0) * 150)   # 基准不适用（债/货/金/海外）→中性
    sc_dd = clamp(55 + mdd * 100)          # 当前回撤越浅越好
    sc_mdd = clamp(55 + maxMdd * 100)      # 历史最大回撤越浅越好
    sc_ir = clamp(55 + (ir if ir is not None else 0.0) * 40)             # 基准不适用 →中性
    sc_avr = clamp(avr)
    # 费率（申购费率%）：越低越好（长期成本关键）
    fee = f.get("feeRate")
    sc_fee = clamp(100 - (fee if fee is not None else 1.0) * 33)
    # 规模（亿元）：适中偏好（<1亿迷你/清盘风险，>800亿 灵活度差）
    if scale is None:
        sc_scale = 50.0
    elif scale < 1: sc_scale = 40
    elif scale < 10: sc_scale = 65
    elif scale < 100: sc_scale = 92
    elif scale < 300: sc_scale = 80
    elif scale < 800: sc_scale = 60
    else: sc_scale = 45
    # 成立年限：≥3 年满分（晨星评级门槛），次新基金扣分
    sc_age = clamp((age_years if age_years is not None else 3.0) / 3.0 * 100)
    # 机构持有占比：5%-70% 加分（<2% 散户化，>70% 波动集中）
    inst = f.get("instPct")
    if inst is None: sc_inst = 50.0
    elif inst < 2: sc_inst = 45
    elif inst < 5: sc_inst = 60
    elif inst < 70: sc_inst = 88
    else: sc_inst = 60
    # 经理任职年限：≥5 年满分（经历牛熊周期的经验价值）
    sc_tenure = clamp((tenure if tenure is not None else 2.0) / 5.0 * 100)

    # 权重（风险调整收益与长期稳健为主导，动量显著下调，避免"追涨"）：
    # 夏普/索提诺/卡玛/Alpha 合计 0.40，长期 0.05，动量仅 0.05；
    # 估值 0.09 仅对指数/基准匹配基金有效；质地(费率/规模/年限/经理/机构)合计 0.18；
    # 月胜率 0.04（稳定性/持有体验，2026-08-28 补强）。
    w = {"avr": 0.10, "mom": 0.05, "long": 0.05, "val": 0.09, "sharpe": 0.10,
         "sortino": 0.11, "calmar": 0.09, "alpha": 0.10, "dd": 0.04, "mdd": 0.05,
         "ir": 0.03, "fee": 0.04, "scale": 0.04, "age": 0.03, "inst": 0.02, "tenure": 0.02,
         "win": 0.04}
    composite = (sc_avr * w["avr"] + sc_mom * w["mom"] + sc_long * w["long"]
                 + sc_val * w["val"] + sc_sharpe * w["sharpe"] + sc_sortino * w["sortino"]
                 + sc_calmar * w["calmar"] + sc_alpha * w["alpha"] + sc_dd * w["dd"]
                 + sc_mdd * w["mdd"] + sc_ir * w["ir"] + sc_fee * w["fee"]
                 + sc_scale * w["scale"] + sc_age * w["age"] + sc_inst * w["inst"]
                 + sc_tenure * w["tenure"] + sc_win * w["win"])

    return {"code": code, "name": f.get("name"), "type": f.get("type", ""),
            "nav": f.get("nav"), "navDate": f.get("navDate"),
            "r20": r20, "r60": r60, "r120": r120, "r250": r250, "r500": r500,
            "vol": vol, "ann": ann, "mdd": mdd, "maxMdd": maxMdd, "sharpe": sharpe,
            "sortino": sortino, "z": z,
            "m": m, "s": s, "mu": mu, "sigma": sigma,
            "beta": beta, "alpha": alpha, "ir": ir, "calmar": calmar,
            "avr": avr, "dims": dims,
            "valPct": vp, "valBasis": val_basis, "benchSecid": secid,
            "managers": managers, "tenure": tenure,
            "fee": fee, "scale": scale, "instPct": inst, "age": age_years, "r3y": r3y, "r2y": r2y,
            "muE": round(muE, 4),
            "winRate": winRate,
            "sc": {"mom": sc_mom, "long": sc_long, "val": sc_val, "sharpe": sc_sharpe,
                   "sortino": sc_sortino, "calmar": sc_calmar, "alpha": sc_alpha, "dd": sc_dd,
                   "mdd": sc_mdd, "ir": sc_ir, "avr": sc_avr, "fee": sc_fee, "scale": sc_scale,
                   "age": sc_age, "inst": sc_inst, "tenure": sc_tenure, "win": sc_win},
            "composite": round(composite, 1)}


def _build_reasons(fac, verdict, cap, amount, capital, p1, p3, p6, p12):
    r = []
    r.append("类型：%s；近一年收益 %s，年化波动 %.1f%%，最大回撤 %s，夏普 %.2f，卡玛 %.2f。"
             % (fac["type"] or "—", _s(fac["r250"]), fac["vol"] * 100,
                _s(fac["mdd"]), fac["sharpe"], fac["calmar"]))
    if fac.get("beta") is None:
        r.append("基准适配：非 A 股权益类，Beta/Alpha/信息比率不适用（不与沪深300比较）。")
    else:
        r.append("相对沪深300：Beta %.2f（%s），年化Alpha %s，信息比率 %.2f。"
                 % (fac["beta"], "高弹性" if fac["beta"] > 1.1 else ("防御" if fac["beta"] < 0.9 else "同步"),
                    _s(fac["alpha"]), fac["ir"]))
    val = fac["valPct"]
    if val is not None:
        r.append("估值分位 %s%%（%s）：%s" % (val, fac["valBasis"],
                 "偏低、具备布局价值" if val < 30 else ("中性" if val < 70 else "偏高、注意追高")))
    else:
        r.append("估值分位：数据不足。")
    if fac["avr"]:
        keys = ["选证能力", "收益率", "抗风险", "稳定性", "择时能力"]
        r.append("东方财富五维综合评分 %.0f（选证/收益/抗风险/稳定/择时 = %s）。"
                 % (fac["avr"], "/".join("%.0f" % (fac["dims"].get(k, 0) or 0) for k in keys)))
    if fac["managers"]:
        tn = fac["tenure"]
        r.append("基金经理：%s%s。"
                 % (fac["managers"][0].get("name", ""), ("（任职 %.1f 年）" % tn) if tn else ""))
    r.append("持有盈利概率：1月 %.0f%% / 3月 %.0f%% / 6月 %.0f%% / 1年 %.0f%%。"
             % (p1 * 100, p3 * 100, p6 * 100, p12 * 100))
    if verdict == "推荐":
        r.append("综合评分 %.0f，多因子较优，可纳入组合。" % fac["composite"])
    elif verdict == "谨慎关注":
        r.append("综合评分 %.0f，存在机会但风险犹存，建议小仓位试探。" % fac["composite"])
    else:
        r.append("综合评分 %.0f，当前性价比较低，建议观望或等待更好买点。" % fac["composite"])
    r.append("建议仓位上限约 %.0f%%（约 ¥%s，按本金 ¥%s 计）。"
             % (cap * 100, format(amount, ","), format(int(capital), ",")))
    return r


# ---------------- 一键配资 / 可执行优化方案 ----------------
def _fund_returns(code, days=730):
    """返回基金最近 250 个交易日的日收益率（累计净值口径，与 _compute_factors 一致，避开分红跳空）。"""
    try:
        ds = get_adj_series(code)
        closes = [d["nav"] for d in ds if d.get("nav") is not None]
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        return rets[-250:]
    except Exception:
        return []


def _corr(a, b):
    n = min(len(a), len(b))
    if n < 20:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = _mean(a), _mean(b)
    da = [x - ma for x in a]
    db = [x - mb for x in b]
    cov = _mean([da[i] * db[i] for i in range(n)])
    va, vb = _std(a), _std(b)
    if va == 0 or vb == 0:
        return None
    return cov / (va * vb)


# 各资产大类对应的 rankhandler ft（用于寻找低相关替代基金）
_REPL_FT = {"股票型": ["gp", "hh", "zs"], "债券型": ["zq"],
            "海外(QDII)": ["qdii"], "黄金": [], "货币型": []}


def _find_replacement(repl, keep, codes, rets_map, topn=8):
    """为相关性过高的 repl 找一个同类替代基金。

    全局相关性约束（修复替换死循环）：候选不仅要与 keep 低相关（≤0.6），
    还必须与组合内【其他所有基金】都低相关（<0.7）——否则换完又和第三只基金高相关，
    重新分析又会提示新的一对，用户陷入"替换→再提示→再替换"死循环。"""
    asset = repl.get("asset")
    fts = _REPL_FT.get(asset, [])
    if not fts:
        return None
    have = set(codes) | {repl.get("code")}
    others = [c for c in codes if c not in (keep.get("code"), repl.get("code"))]
    cands = []
    for ft in fts:
        try:
            rows = get_rank_list(ft, 40)
        except Exception:
            rows = []
        for code, name, y1, ed in rows:
            if code in have or y1 < 0:
                continue
            cands.append((code, name, y1))
    if not cands:
        return None
    cands.sort(key=lambda x: x[2], reverse=True)
    keep_rets = rets_map.get(keep.get("code"), [])
    best = None
    best_cr = 2.0
    best_score = -1
    for code, name, y1 in cands[:topn * 2]:
        cr = _corr(_fund_returns(code, 730), keep_rets)
        if cr is None or cr > 0.6:
            continue
        # 全局约束：与组合内其他所有基金相关性 <0.7（有一只觉得高相关即放弃该候选）
        ok = True
        for oc in others:
            ocr = _corr(_fund_returns(code, 730), rets_map.get(oc, []))
            if ocr is not None and ocr >= 0.7:
                ok = False
                break
        if not ok:
            continue
        try:
            fac = _compute_factors(code)
        except Exception:
            fac = None
        score = (fac or {}).get("composite") or 0
        if score < 55:
            continue
        if best is None or cr < best_cr or (abs(cr - best_cr) < 1e-6 and score > best_score):
            best = ((code, name, y1), cr)
            best_cr = cr
            best_score = score
    if not best:
        return None
    (code, name, y1), cr = best
    return {"code": code, "name": name, "corr": round(cr, 2),
            "reason": "近1年收益 +%.0f%%，多因子综合分 %d，与保留基金相关系数仅 %.2f，且与组合内其他基金均低相关，替换后不会再次触发高相关预警。" % (y1, best_score, cr)}


def _norm_asset(a):
    """把细分类型归并到大类资产桶，与 recommend_portfolio 的 alloc 口径对齐。
    否则'混合型/指数型'在持仓里单列，会绕过权益集中度预警。"""
    if not a:
        return a
    if "股票" in a or "混合" in a or "指数" in a:
        return "股票型"
    if "QDII" in a or "海外" in a:
        return "海外(QDII)"
    if "债券" in a:
        return "债券型"
    if "货币" in a:
        return "货币型"
    if "黄金" in a:
        return "黄金"
    return a


def build_allocate(funds, principal, existing=None):
    """一键配资核心：返回配置金额、相关性矩阵、可执行优化（含具体替换方案）、买入方案。
    funds: [{code,name,weight,asset}]；principal: 本金（元）。"""
    items, rets_map = [], {}
    total_w = sum(float(f.get("weight") or 0) for f in funds)
    for f in funds:
        code = f.get("code")
        if not code:
            continue
        w = (float(f.get("weight") or 0) / total_w) if total_w else (1.0 / max(1, len(funds)))
        amount = round(principal * w, 2)
        try:
            fac = _compute_factors(code)
        except Exception:
            fac = None
        rets_map[code] = _fund_returns(code, 730)
        items.append({"code": code, "name": f.get("name"), "asset": _norm_asset(f.get("asset")),
                      "weight": round(w, 4), "amount": amount,
                      "type": (fac or {}).get("type", ""), "composite": (fac or {}).get("composite"),
                      "valPct": (fac or {}).get("valPct")})
    codes = [it["code"] for it in items]

    # 现有持仓（来自“我的组合”）作为背景，一并纳入相关性与集中度分析
    exist_items = []
    if existing:
        for h in existing:
            c = h.get("code")
            if not c:
                continue
            exist_items.append({"code": c, "name": h.get("name"),
                                "asset": _norm_asset(h.get("asset") or h.get("type") or ""),
                                "value": float(h.get("value") or 0)})
    exist_codes = [e["code"] for e in exist_items]
    all_codes = codes + exist_codes
    for e in exist_items:
        rets_map.setdefault(e["code"], _fund_returns(e["code"], 730))

    # 相关性矩阵：新选 + 现有 全量
    corr = {}
    for i in range(len(all_codes)):
        for j in range(i + 1, len(all_codes)):
            c = _corr(rets_map.get(all_codes[i]), rets_map.get(all_codes[j]))
            if c is not None:
                corr["%s|%s" % (all_codes[i], all_codes[j])] = round(c, 3)

    # 可执行优化：高相关性 → 具体替换方案（区分“新选内部”与“新选 vs 已有”）
    opts = []
    handled = set()
    for c, key in sorted(((v, k) for k, v in corr.items()), reverse=True):
        if c < 0.8:
            break
        a, b = key.split("|")
        if a in handled or b in handled:
            continue
        a_new, b_new = a in codes, b in codes
        a_ex, b_ex = a in exist_codes, b in exist_codes
        if a_ex and b_ex:
            continue  # 已有 vs 已有 由“我的组合”诊断单独处理
        if a_new and b_new:
            # 新选内部高相关：保留评分高者，替换另一只
            fa = next(x for x in items if x["code"] == a)
            fb = next(x for x in items if x["code"] == b)
            sa = fa.get("composite") or 50
            sb = fb.get("composite") or 50
            keep, repl = (fa, fb) if sa >= sb else (fb, fa)
            rep = _find_replacement(repl, keep, all_codes, rets_map)
            handled.add(keep["code"]); handled.add(repl["code"])
            if rep:
                opts.append({"type": "corr", "level": "warn",
                    "text": "%s 与 %s 相关系数 %.2f，几乎同涨同跌，持有两只等于押注同一方向。"
                             % (fa["name"], fb["name"], c),
                    "replace": {"from": {"code": repl["code"], "name": repl["name"]},
                                "to": {"code": rep["code"], "name": rep["name"], "corr": rep["corr"],
                                       "asset": repl["asset"],
                                       "link": "https://fund.eastmoney.com/%s.html" % rep["code"]},
                                "oldCorr": round(c, 2), "newCorr": rep["corr"], "reason": rep["reason"]}})
            else:
                opts.append({"type": "corr", "level": "warn",
                    "text": "%s 与 %s 相关系数 %.2f，且同类候选与组合内其他基金也普遍高相关（同赛道/同指数基金天然同涨同跌），继续替换同类没有意义。建议二选一保留评分更高者，将另一只的资金改配债券型 / 货币型等低相关资产。"
                             % (fa["name"], fb["name"], c)})
        else:
            # 一侧新选、一侧已有：保留已持有的，替换新选的
            new_code, ex_code = (a, b) if a_new else (b, a)
            nf = next(x for x in items if x["code"] == new_code)
            ef = next(x for x in exist_items if x["code"] == ex_code)
            rep = _find_replacement(nf, ef, all_codes, rets_map)
            handled.add(new_code); handled.add(ex_code)
            if rep:
                opts.append({"type": "corr", "level": "warn",
                    "text": "你已持有 %s（%s），与本次选中的 %s（%s）相关系数 %.2f，高度同涨同跌。建议保留你已持有的，将后者更换为低相关品种。"
                             % (ef["name"], ef["code"], nf["name"], nf["code"], c),
                    "replace": {"from": {"code": nf["code"], "name": nf["name"]},
                                "to": {"code": rep["code"], "name": rep["name"], "corr": rep["corr"],
                                       "asset": nf["asset"],
                                       "link": "https://fund.eastmoney.com/%s.html" % rep["code"]},
                                "oldCorr": round(c, 2), "newCorr": rep["corr"], "reason": rep["reason"]}})
            else:
                opts.append({"type": "corr", "level": "warn",
                    "text": "你已持有 %s（%s），与本次选中的 %s（%s）相关系数 %.2f，且同类候选与组合内其他基金也普遍高相关，继续换同类易陷入循环。建议二选一保留（优先保留已持有的），将另一只的资金改配债券型 / 货币型等低相关资产。"
                             % (ef["name"], ef["code"], nf["name"], nf["code"], c)})

    # 重复提示：本次选的已在组合里持有
    for it in items:
        if it["code"] in exist_codes:
            opts.append({"type": "dup", "level": "warn", "code": it["code"],
                "text": "你已持有 %s（%s），本次又选了同一只，建议从本次方案中移除重复，避免重复建仓。"
                         % (it["name"], it["code"])})

    # 合并集中度预警（结合已有持仓市值）
    existing_total = sum(e["value"] for e in exist_items)
    combined_total = existing_total + principal
    if combined_total > 0:
        by_asset = {}
        for it in items:
            by_asset[it["asset"]] = by_asset.get(it["asset"], 0) + it["amount"]
        for e in exist_items:
            by_asset[e["asset"]] = by_asset.get(e["asset"], 0) + e["value"]
        equity = by_asset.get("股票型", 0) + by_asset.get("海外(QDII)", 0)
        if equity / combined_total > 0.6:
            exist_val = sum(e["value"] for e in exist_items if e["asset"] in ("股票型", "海外(QDII)"))
            new_val = sum(it["amount"] for it in items if it["asset"] in ("股票型", "海外(QDII)"))
            over = principal * (equity / combined_total - 0.6)
            opts.append({"type": "concentration", "level": "warn",
                "text": "合并你已有持仓后，权益类（股票型+海外QDII）占组合 %.0f%%（已有 ¥%s + 本次 ¥%s），过于集中。建议降至 60%% 以内，本次可将约 ¥%s 改配债券型 / 货币型等防御资产。"
                         % (equity / combined_total * 100,
                            format(round(exist_val), ","), format(round(new_val), ","),
                            format(round(over), ","))})

    # 买入方案（具体到金额 + 估值策略 + 购买入口）
    buy = []
    for it in items:
        vp = it.get("valPct")
        if vp is None:
            strat = "估值分位数据不足，建议按周定投、分批建仓以平滑成本。"
        elif vp > 70:
            strat = "估值分位 %.0f%% 偏高，宜分 3 批逢回调买入，切忌一次性追高。" % vp
        elif vp < 30:
            strat = "估值分位 %.0f%% 偏低，具备布局价值，可一次性或加大建仓。" % vp
        else:
            strat = "估值分位 %.0f%% 中性，建议按周定投分批买入。" % vp
        buy.append({"code": it["code"], "name": it["name"], "asset": it["asset"], "type": it["type"],
                    "weight": it["weight"], "amount": it["amount"], "strategy": strat,
                    "verdict": None, "composite": it["composite"],
                    "link": "https://fund.eastmoney.com/%s.html" % it["code"]})

    return {"ok": True, "principal": principal, "items": items, "corr": corr,
            "optimizations": opts, "buyPlan": buy,
            "generatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}


# ---------------- 单基金诊断：图表与对比数据增强 ----------------
def _get_f10_stage(code):
    """东方财富 F10 阶段涨幅表：本基金 / 同类平均 / 沪深300 三列。
    数据源 FundArchivesDatas.aspx?type=jdzf（datatype 路径已 404）。返回 {阶段名:{fund,peer,csi}}。"""
    url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jdzf&code=%s" % code
    try:
        txt = fetch(url, headers={"Referer": "https://fundf10.eastmoney.com/",
                                  "User-Agent": "Mozilla/5.0"}, timeout=12)
    except Exception:
        return {}
    m = re.search(r"content:\"(.*?)\"", txt, re.S)
    if not m:
        return {}
    content = m.group(1).replace("\\/", "/").replace('\\"', '"')
    uls = re.findall(r"<ul[^>]*>(.*?)</ul>", content, re.S)
    out = {}
    for ul in uls:
        if "fcol" in ul[:60]:   # 跳过表头 ul
            continue
        tm = re.search(r"<li class='title'>([^<]*)</li>", ul)
        if not tm:
            continue
        label = tm.group(1).strip()
        vals = re.findall(r">(-?\d+\.?\d*)%</li>", ul)
        if len(vals) < 3:
            vals = [v for v in re.findall(r">(-?\d+\.?\d*)%", ul)[:3]]
        if len(vals) < 3:
            continue

        def _p(x):
            try:
                return float(x)
            except Exception:
                return None
        out[label] = {"fund": _p(vals[0]), "peer": _p(vals[1]), "csi": _p(vals[2])}
    return out


def _enrich_diagnosis(code):
    """返回诊断图表所需数据：净值累计走势、阶段涨幅(本基金/同类/沪深300)、
    年度涨幅、季度涨幅。同类平均来自 F10（失败则 null，前端降级）。"""
    hist = get_history(code, 1500)
    data = hist.get("data") or []
    dates = [d["date"] for d in data]
    closes = [float(d["nav"]) for d in data]
    n = len(closes)
    csi = cached("index", "1.000300", _daily_ttl(), lambda: _get_index_series("1.000300", 1500))
    csi_dates = csi.get("dates") or []
    csi_vals = [float(v) for v in (csi.get("values") or [])]

    # ---- 净值累计涨幅走势（本基金 vs 沪深300，起点=100）----
    f10 = cached("f10stage", code, _daily_ttl(), lambda: _get_f10_stage(code))
    nav_trend = {"dates": dates, "fund": [], "csi": []}
    if n:
        base = closes[0]
        nav_trend["fund"] = [round(c / base * 100 - 100, 2) for c in closes]
    if csi_vals:
        c0 = csi_vals[0]
        ji = 0
        for dt in dates:
            while ji < len(csi_dates) and csi_dates[ji] < dt:
                ji += 1
            k = ji - 1
            if k < 0:
                nav_trend["csi"].append(None)   # 指数数据起始前的区间留空，避免折线被钉成 0
            else:
                nav_trend["csi"].append(round(csi_vals[k] / c0 * 100 - 100, 2))
    else:
        nav_trend["csi"] = [None] * n

    # ---- 区间定位辅助 ----
    def idx_after(target):
        for i, d in enumerate(dates):
            if d >= target:
                return i
        return n - 1
    def idx_before(target):
        res = 0
        for i, d in enumerate(dates):
            if d <= target:
                res = i
            else:
                break
        return res
    def csi_idx_after(target):
        for i, d in enumerate(csi_dates):
            if d >= target:
                return i
        return len(csi_vals) - 1
    def csi_idx_before(target):
        res = 0
        for i, d in enumerate(csi_dates):
            if d <= target:
                res = i
            else:
                break
        return res

    # ---- 阶段涨幅（本基金 computed；同类/沪深300 优先 F10，否则 computed）----
    def stage_fund(k):
        return round(closes[-1] / closes[-1 - k] - 1, 4) if n > k else None
    def stage_csi(k):
        return round(csi_vals[-1] / csi_vals[-1 - k] - 1, 4) if len(csi_vals) > k else None
    def f10v(label):
        e = f10.get(label)
        if not e:
            return (None, None, None)
        return (e["fund"], e["peer"], e["csi"])

    stage_growth = []
    for label, k in [("近1周", 5), ("近1月", 21), ("近3月", 63), ("近6月", 126),
                     ("近1年", 252), ("近2年", 504), ("近3年", 756)]:
        fr = stage_fund(k)
        cr = stage_csi(k)
        pf, pp, pc = f10v(label)
        peer = (pp / 100.0) if pp is not None else None
        csi_v = (pc / 100.0) if pc is not None else cr
        stage_growth.append({"label": label, "fund": fr, "peer": peer, "csi": csi_v})
    if n:  # 成立以来
        fr = round(closes[-1] / closes[0] - 1, 4)
        cr = round(csi_vals[-1] / csi_vals[0] - 1, 4) if csi_vals else None
        pf, pp, pc = f10v("成立以来")
        peer = (pp / 100.0) if pp is not None else None
        csi_v = (pc / 100.0) if pc is not None else cr
        stage_growth.append({"label": "成立以来", "fund": fr, "peer": peer, "csi": csi_v})

    # ---- 年度涨幅（近 3 个完整自然年）----
    years = sorted({int(d[:4]) for d in dates})
    full_years = [y for y in years if y < max(years)] if years else []
    full_years = full_years[-3:]
    year_growth = []
    for y in full_years:
        s_i = idx_after("%d-01-01" % y)
        e_i = idx_before("%d-12-31" % y)
        fr = round(closes[e_i] / closes[s_i] - 1, 4) if e_i > s_i else None
        cs = csi_idx_after("%d-01-01" % y)
        ce = csi_idx_before("%d-12-31" % y)
        cr = round(csi_vals[ce] / csi_vals[cs] - 1, 4) if (csi_vals and ce > cs) else None
        pp = f10.get("%d年" % y, {}).get("peer")
        peer = (pp / 100.0) if pp is not None else None
        year_growth.append({"year": y, "fund": fr, "peer": peer, "csi": cr})

    # ---- 季度涨幅（近 4 个季度）----
    def qend(y, m):
        if m <= 3:
            return (y - 1, 12, 31)
        if m <= 6:
            return (y, 3, 31)
        if m <= 9:
            return (y, 6, 30)
        return (y, 9, 30)
    q_res = []
    if dates:
        y, m = int(dates[-1][:4]), int(dates[-1][5:7])
        for _ in range(4):
            ey, em, ed = qend(y, m)
            py, pm, pd = qend(ey, em)
            end_i = idx_before("%04d-%02d-%02d" % (ey, em, ed))
            start_i = idx_before("%04d-%02d-%02d" % (py, pm, pd))
            fr = round(closes[end_i] / closes[start_i] - 1, 4) if end_i > start_i else None
            q_res.append({"label": "%dQ%d" % (ey, em // 3), "fund": fr})
            y, m = ey, em
        q_res.reverse()
    quarter_growth = q_res

    return {"navTrend": nav_trend, "stageGrowth": stage_growth,
            "yearGrowth": year_growth, "quarterGrowth": quarter_growth,
            "peerAvailable": any((s.get("peer") is not None) for s in stage_growth)}


def advise_fund(code, capital=100000):
    fac = _compute_factors(code)
    if fac is None:
        return {"ok": False, "error": "未找到基金或历史净值不足 60 个交易日，无法稳健评估"}
    # 盈利概率与推荐接口同框架：用均值回归预期收益 μE（非历史漂移 μ），
    # 避免“历史涨→概率高”的外推偏差，也保证诊断与推荐口径一致。
    mu, sigma = fac.get("muE"), fac.get("sigma")
    if mu is None or sigma is None or sigma <= 0:
        mu, sigma = fac.get("mu"), fac.get("sigma")
    if mu is None or sigma is None or sigma <= 0:
        # 无收益序列（异常兜底）：货币类收益极稳，盈利概率近似 100%
        p1, p3, p6, p12 = 0.999, 0.999, 0.999, 0.999
    else:
        # 对数收益蒙特卡洛：g=Σ ln(1+r)，g>0 ⇔ 持有期满盈利。
        # 几何漂移 ln 均值天然含复利拖累（μ≈m−σ²/2），不会像算术版那样系统性高估；
        # 对数累加数值稳定，也不会出现 1+r<0 的负净值路径。
        def mc(days, sims=2000):
            win_cnt = 0
            for _ in range(sims):
                g = 0.0
                for _ in range(days):
                    g += _norm_box(mu, sigma)
                if g > 0:
                    win_cnt += 1
            return win_cnt / sims

        p1, p3, p6, p12 = mc(21), mc(63), mc(126), mc(252)

    composite = fac["composite"]
    # 评级阈值与推荐接口同源（_RECO_TH，recommend_portfolio 每次按分布自适应更新），
    # 保证同一基金在「推荐」与「诊断」里结论一致。
    verdict = "推荐" if composite >= _RECO_TH["reco"] else ("谨慎关注" if composite >= _RECO_TH["watch"] else "暂不推荐")

    # 仓位建议：按波动风险控制单只上限；估值过高（>80%）减仓
    if fac["vol"] < 0.10:
        cap = 0.25
    elif fac["vol"] < 0.20:
        cap = 0.18
    elif fac["vol"] < 0.30:
        cap = 0.12
    else:
        cap = 0.07
    if fac["valPct"] and fac["valPct"] > 80:
        cap *= 0.6
    if verdict == "暂不推荐":
        cap = min(cap, 0.05)
    amount = int(cap * capital)

    reasons = _build_reasons(fac, verdict, cap, amount, capital, p1, p3, p6, p12)
    enr = _enrich_diagnosis(code)

    return {"ok": True, "code": code, "name": fac["name"], "type": fac["type"],
            "nav": fac["nav"], "navDate": fac["navDate"], "score": composite, "verdict": verdict,
            "cap": cap, "amount": amount, "capital": capital,
            "r20": fac["r20"], "r60": fac["r60"], "r120": fac["r120"], "r250": fac["r250"],
            "vol": fac["vol"], "ann": fac["ann"], "mdd": fac["mdd"], "sharpe": fac["sharpe"], "z": fac["z"],
            "beta": fac["beta"], "alpha": fac["alpha"], "ir": fac["ir"], "calmar": fac["calmar"],
            "avr": fac["avr"], "dims": fac["dims"],
            "valPct": fac["valPct"], "valBasis": fac["valBasis"],
            "managers": fac["managers"], "tenure": fac["tenure"],
            "p1": p1, "p3": p3, "p6": p6, "p12": p12, "reasons": reasons,
            "navTrend": enr["navTrend"], "stageGrowth": enr["stageGrowth"],
            "yearGrowth": enr["yearGrowth"], "quarterGrowth": enr["quarterGrowth"],
            "peerAvailable": enr["peerAvailable"]}


# ---------------- HTTP 服务 ----------------

class Handler(http.server.BaseHTTPRequestHandler):
    # 关键：腾讯云函数 URL 透传需 HTTP/1.1。若用默认 HTTP/1.0，网关透传后浏览器
    # 会把 text/html 当成文件附件直接下载（fetch 调用正常、浏览器打开却下载）。
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # 关键：覆盖腾讯云函数 URL 网关默认注入的 Content-Disposition: attachment，
        # 否则浏览器会把网页/JSON 当附件下载而不是渲染。
        self.send_header("Content-Disposition", "inline")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS 预检：前端静态托管(COS)跨域调用本函数 URL 时，浏览器先发 OPTIONS，
        # 必须返回 200 + CORS 头，否则 POST /api/* 会被浏览器拦截。
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", "inline")
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == "/debug":
            info = {
                "python_version": sys.version.split()[0],
                "PORT_env": os.environ.get("PORT"),
                "protocol_version": "HTTP/1.1",
                "trading_now": _is_trading_now(),
                "next_nav_refresh": _next_nav_refresh().strftime("%Y-%m-%d %H:%M"),
                "cache_entries": len(_CACHE),
            }
            self._send(200, json.dumps(info, ensure_ascii=False, indent=2))
            return
        if path in ("/", "/index.html"):
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if path == "/static/chart.umd.js":
            with open(os.path.join(BASE, "static", "chart.umd.js"), "rb") as f:
                self._send(200, f.read(), "application/javascript; charset=utf-8")
            return
        if path == "/api/search":
            self._send(200, json.dumps(search_fund(qs.get("key", [""])[0]), ensure_ascii=False))
            return
        if path == "/api/zhuhai-market":
            self._send(200, json.dumps(fetch_zhuhai_market(), ensure_ascii=False))
            return
        if path == "/api/market":
            self._send(200, json.dumps(get_market_env(), ensure_ascii=False))
            return
        if path == "/api/recommend":
            # 动态全市场扫描较重，结果缓存 30 分钟（数据日频变化，足够动态且避免每次点击都大扫描拖垮服务）
            try:
                data = cached("reco", "dyn", 1800, recommend_portfolio)
            except Exception as e:
                data = {"ok": False, "error": str(e), "funds": [], "note": "推荐计算异常，请稍后重试。"}
            self._send(200, json.dumps(data, ensure_ascii=False))
            return
        if path == "/api/recommend-backtest":
            # 回测较重，结果缓存 12h（数据日频，足够且避免每次点击都大扫描拖垮服务）
            # 支持 ?codes=110011,163415[&weights=0.3,0.3,0.4] 回测用户指定的组合（可带权重，
            # 如按持仓市值占比回测"我的持仓"）；不传 codes 则回测推荐组合
            try:
                codes = qs.get("codes", [""])[0]
                codes = [c for c in codes.replace("，", ",").split(",") if c.strip()]
                weights = None
                if codes:
                    wq = qs.get("weights", [""])[0]
                    wl = [w for w in wq.replace("，", ",").split(",") if w.strip()]
                    try:
                        if len(wl) == len(codes):
                            weights = [float(w) for w in wl]
                    except Exception:
                        weights = None
                    data = cached("reco", "btcodes:" + ",".join(codes) + ("|" + ",".join(wl) if wl else ""), 43200,
                                  lambda: recommend_backtest_codes(codes, weights))
                else:
                    data = cached("reco", "backtest", 43200, recommend_backtest)
            except Exception as e:
                data = {"ok": False, "error": str(e), "results": []}
            self._send(200, json.dumps(data, ensure_ascii=False))
            return
        if path.startswith("/api/advise/"):
            code = path.split("/")[-1]
            try:
                capital = int(qs.get("capital", ["100000"])[0])
            except Exception:
                capital = 100000
            self._send(200, json.dumps(advise_fund(code, capital), ensure_ascii=False))
            return
        if path.startswith("/api/index/"):
            code = path.split("/")[-1]
            secid = {"000300": "1.000300", "000905": "1.000905",
                     "399006": "0.399006"}.get(code, "1." + code)
            name = {"000300": "沪深300", "000905": "中证500", "399006": "创业板指"}.get(code, code)
            data = cached("index", secid, _daily_ttl(), lambda: _get_index_series(secid, 800))
            data = dict(data)
            data["name"] = name
            self._send(200, json.dumps(data, ensure_ascii=False))
            return
        if path.startswith("/api/fund/"):
            self._send(200, json.dumps(get_fund(path.split("/")[-1]), ensure_ascii=False))
            return
        if path.startswith("/api/history/"):
            code = path.split("/")[-1]
            days = int(qs.get("days", ["365"])[0])
            self._send(200, json.dumps(get_history(code, days), ensure_ascii=False))
            return
        if path.startswith("/api/holdings/"):
            self._send(200, json.dumps(get_holdings(path.split("/")[-1]), ensure_ascii=False))
            return
        if path.startswith("/api/"):
            self._send(404, json.dumps({"error": "not found"}))
            return
        # SPA 兜底：其余未匹配路径（兼容函数 URL 可能带上的路径前缀）统一返回首页
        with open(os.path.join(BASE, "index.html"), "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        try:
            if path == "/api/allocate":
                funds = body.get("funds") or []
                try:
                    principal = float(body.get("principal") or 0)
                except Exception:
                    principal = 0.0
                data = build_allocate(funds, principal, body.get("existing"))
            elif path == "/api/optimize":
                # 组合诊断：用现有持仓权重（无权重则等权），principal=0 仅算相关性与替换方案
                funds = body.get("funds") or []
                data = build_allocate(funds, 0)
            else:
                data = {"ok": False, "error": "unknown endpoint"}
            self._send(200, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            self._send(200, json.dumps({"ok": False, "error": str(e)}))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print("Fund Portfolio Analyzer running at http://localhost:%d" % PORT)
        httpd.serve_forever()
