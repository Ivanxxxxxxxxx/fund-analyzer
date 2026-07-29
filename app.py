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
    """现任基金经理：名称 + 任职起始日（用于计算任职年限）。"""
    m = re.search(r'Data_currentFundManager\s*=\s*(\[.*?\]);', txt, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
        out = []
        for x in arr:
            if not isinstance(x, dict):
                continue
            name = x.get("name") or x.get("xm") or ""
            sdate = x.get("sdate") or x.get("beginDate") or ""
            out.append({"name": name, "sdate": str(sdate)})
        return out
    except Exception:
        return []


def _get_detail_raw(code):
    """基金档案：名称/类型/最新净值+涨跌/资产配置/前十大持仓/五维评分/经理。"""
    res = {"ok": True, "code": code, "name": "", "type": "", "nav": None,
           "navDate": None, "changePct": None,
           "assetAllocation": [], "industry": [], "holdings": [],
           "perfEval": None, "managers": []}
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
            "managers": detail.get("managers") or []}


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


# 用于判断市场环境的宽基/海外/避险指数（东方财富 secid）
INDEX_MAP = {
    "沪深300": "1.000300",
    "中证500": "1.000905",
    "创业板指": "0.399006",
    "纳斯达克": "100.IXIC",
    "标普500": "100.SPX",
    "黄金ETF": "1.518880",
}


def _get_index_kline(secid, days=300):
    """指数日线收盘序列（多 host 重试，失败容错返回 [] 而非抛异常）。"""
    hosts = ["https://push2his.eastmoney.com", "https://push2.eastmoney.com", "https://quote.eastmoney.com"]
    for host in hosts:
        try:
            url = host + ("/api/qt/stock/kline/get?secid=%s&fields1=f1&fields2=f51,f53&klt=101&fqt=1&beg=0&end=20500101" % secid)
            txt = fetch(url, headers={"Referer": "https://quote.eastmoney.com/"}, timeout=15)
            if not txt:
                continue
            d = json.loads(txt)
            klines = (d.get("data") or {}).get("klines") or []
            rows = []
            for k in klines:
                parts = k.split(",")
                if len(parts) >= 2:
                    try:
                        rows.append(float(parts[1]))
                    except Exception:
                        pass
            if rows:
                return rows[-days:]
        except Exception:
            continue
    return []


def _get_index_series(secid, days=800):
    """返回指数日期序列与收盘值，用于组合走势基准对比（多 host 重试容错）。"""
    hosts = ["https://push2his.eastmoney.com", "https://push2.eastmoney.com", "https://quote.eastmoney.com"]
    for host in hosts:
        try:
            url = host + ("/api/qt/stock/kline/get?secid=%s&fields1=f1&fields2=f51,f53&klt=101&fqt=1&beg=0&end=20500101" % secid)
            txt = fetch(url, headers={"Referer": "https://quote.eastmoney.com/"}, timeout=15)
            if not txt:
                continue
            d = json.loads(txt)
            klines = (d.get("data") or {}).get("klines") or []
            dates, values = [], []
            for k in klines:
                parts = k.split(",")
                if len(parts) >= 2:
                    try:
                        dates.append(parts[0])
                        values.append(float(parts[1]))
                    except Exception:
                        pass
            if values:
                if days:
                    dates, values = dates[-days:], values[-days:]
                return {"dates": dates, "values": values}
        except Exception:
            continue
    return {"dates": [], "values": []}


def _bench_daily_returns(days=730):
    """沪深300 日收益率序列（用于 Alpha/Beta/信息比率计算），日频缓存。"""
    rows = _get_index_kline("1.000300", days)
    if len(rows) < 2:
        return []
    return [rows[i] / rows[i - 1] - 1 for i in range(1, len(rows))]


def _index_valuation_percentile(secid, years=5):
    """指数估值分位：以近 N 年收盘价的百分位近似（海外节点取不到官方 PE 序列时的稳健代理）。
    返回 0-100，越高越贵；样本不足返回 None。"""
    rows = _get_index_kline(secid, max(60, int(years * 252)))
    if len(rows) < 60:
        return None
    cur = rows[-1]
    below = sum(1 for x in rows if x <= cur)
    return round(below / len(rows) * 100, 1)


def _market_from_fund_breadth():
    """海外节点取不到指数日线时，用实时开放式基金榜单的收益率广度推断市场温度。
    返回 ok=True 的精简市场状态；样本不足时返回 None。完全依赖可达的 rankhandler 接口。"""
    rets = []
    for ft in ("gp", "hh", "zs"):
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


def get_rank_list(ft, pn=100, name_filter=None):
    """动态拉取天天基金开放式基金排行，返回 [(code, name, y1, est_date)]。
    彻底替代内置名单：每次调用都是实时榜单。"""
    sd = (datetime.date.today() - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
    ed = datetime.date.today().strftime("%Y-%m-%d")
    dt = "money" if ft == "money" else "kf"
    url = ("https://fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=%s&ft=%s&rs=&gs=0"
           "&sc=1nz&st=desc&sd=%s&ed=%s&qdii=&tabSubtype=,,,&pi=1&pn=%d&dx=1&v=%s"
           ) % (dt, ft, sd, ed, pn, random.random())
    try:
        html = fetch(url, headers={"Referer": "https://fund.eastmoney.com/"}, timeout=20)
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


def recommend_portfolio():
    env = get_market_env()
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

    # === 动态候选：实时拉取全市场榜单（不再使用内置名单） ===
    bench = cached("bench", "hs300", _daily_ttl(), lambda: _bench_daily_returns(730))
    now = datetime.date.today()
    candidates = []
    empty_cats = []

    def _est_years(d):
        try:
            y, mo, dd = map(int, d.split("-"))
            return (now - datetime.date(y, mo, dd)).days / 365.25
        except Exception:
            return 99.0

    for ft, cat_label, asset, topn, need_age in _RECO_CATS:
        try:
            if ft == "__gold":
                rows = get_rank_list("all", 2000, name_filter="黄金")
            elif ft == "__money":
                rows = get_rank_list("money", 30)
                if not rows:
                    rows = get_rank_list("all", 400, name_filter="货币")
            else:
                rows = get_rank_list(ft, 100)
        except Exception:
            rows = []
        if need_age:
            rows = [r for r in rows if _est_years(r[3]) >= 2.0]
        rows.sort(key=lambda r: r[2], reverse=True)
        head = rows[:topn]
        for code, name, y1, ed in head:
            candidates.append((cat_label, asset, code, name))
        if not head:
            empty_cats.append(cat_label)

    # === 多因子精评（实时净值/五维/经理/估值） ===
    def _score(item):
        cat_label, asset, code, name = item
        try:
            fac = _compute_factors(code, bench)
            return (cat_label, asset, fac) if fac else None
        except Exception:
            return None

    scored = []
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            for r in ex.map(_score, candidates):
                if r:
                    scored.append(r)
    except Exception:
        for it in candidates:
            r = _score(it)
            if r:
                scored.append(r)

    by_cat = {}
    for cat_label, asset, fac in scored:
        by_cat.setdefault(cat_label, []).append((asset, fac))
    funds = []
    for cat_label, lst in by_cat.items():
        lst.sort(key=lambda x: x[1]["composite"], reverse=True)
        top = lst[:3]
        asset = top[0][0]
        w = alloc.get(asset, 0.0)
        per = w / len(top) if top else 0.0
        for asset, fac in top:
            verdict = "推荐" if fac["composite"] >= 68 else ("谨慎关注" if fac["composite"] >= 50 else "暂不推荐")
            reasons = ["多因子综合 %.0f 分；近一年 %s、夏普 %.2f、最大回撤 %s、估值分位 %s%%。"
                       % (fac["composite"], _s(fac["r250"]), fac["sharpe"], _s(fac["mdd"]),
                          (fac["valPct"] if fac["valPct"] is not None else 0))]
            if fac["avr"]:
                reasons.append("东方财富五维综合 %.0f。" % fac["avr"])
            funds.append({"category": cat_label, "asset": asset, "code": fac["code"], "name": fac["name"],
                          "type": fac["type"], "score": fac["composite"], "verdict": verdict,
                          "weight": round(per, 4), "valPct": fac["valPct"], "reasons": reasons})
    funds.sort(key=lambda x: (x["asset"], -x["score"]))
    dyn_note = ""
    if empty_cats:
        dyn_note = "（%s 的实时榜单在本节点暂不可用，本次该仓位为空，可手动添加对应基金）" % "、".join(empty_cats)
    return {"ok": True, "env": env, "alloc": alloc, "note": note + dyn_note,
            "universe": len(candidates), "funds": funds, "dynamic": True,
            "generatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}



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


def _compute_factors(code, bench_rets=None):
    """多因子计算：收益/风险/风险调整收益/Alpha-Beta/卡玛/信息比率/估值分位/东财五维/经理任职。
    各因子归一化为 0-100 贡献后加权得到 composite。"""
    f = get_fund(code)
    if not f.get("ok") or not f.get("name"):
        return None
    hist = get_history(code, 730)
    data = hist.get("data") or []
    if len(data) < 60:
        return None
    closes = [d["nav"] for d in data]
    n = len(closes)
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, n)]
    m, s = _mean(rets), _std(rets)

    def ret(k):
        return closes[-1] / closes[-1 - k] - 1 if n > k else 0.0

    r20, r60, r120, r250 = ret(20), ret(60), ret(120), ret(250)
    vol = s * _sqrt(252)
    ann = (closes[-1] / closes[0]) ** (252.0 / len(rets)) - 1
    peak = max(closes)
    mdd = closes[-1] / peak - 1
    sharpe = (ann - 0.02) / vol if vol > 0 else 0.0
    win = closes[-120:]
    mu, sd = _mean(win), _std(win) or 1e-9
    z = (closes[-1] - mu) / sd

    # 与沪深300回归：Beta / 年化Alpha / 信息比率
    if bench_rets is None:
        bench_rets = cached("bench", "hs300", _daily_ttl(), lambda: _bench_daily_returns(730))
    L = min(len(rets), len(bench_rets))
    if L >= 30:
        fr = rets[-L:]
        br = bench_rets[-L:]
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

    # 东方财富五维评分
    pe = f.get("perfEval") or {}
    avr = pe.get("avr") or 0.0
    dims = pe.get("dims") or {}

    # 估值分位（固收/货币类不适用估值分位，置空避免误导）
    ftype = f.get("type", "") or ""
    secid = _fund_bench_secid(f.get("name", ""), ftype)
    if "债券" in ftype or "货币" in ftype:
        vp = None
        val_basis = "不适用（固收/货币类）"
    elif secid:
        try:
            vp = cached("val", "f_" + secid, _daily_ttl(),
                        lambda s=secid: _index_valuation_percentile(s, 5))
        except Exception:
            vp = None
        val_basis = "基准指数近5年价格分位"
    else:
        vp = round(sum(1 for x in closes if x <= closes[-1]) / len(closes) * 100, 1)
        val_basis = "自身净值近3年分位（主动股基估值代理）"

    # 基金经理任职年限
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

    # 因子归一化 → 0-100 贡献
    def clamp(x):
        return max(0.0, min(100.0, x))

    sc_mom = clamp(50 + r250 * 100 * 1.5)
    sc_val = clamp(100 - (vp if vp is not None else 50))
    sc_sharpe = clamp(sharpe / 2.0 * 100)
    sc_calmar = clamp(calmar / 3.0 * 100)
    sc_alpha = clamp(50 + alpha * 100 * 2)
    sc_dd = clamp(50 + mdd * 100)
    sc_ir = clamp(50 + ir * 50)
    sc_avr = clamp(avr)

    w = {"avr": 0.20, "mom": 0.18, "val": 0.15, "sharpe": 0.12,
         "calmar": 0.12, "alpha": 0.12, "dd": 0.06, "ir": 0.05}
    composite = (sc_avr * w["avr"] + sc_mom * w["mom"] + sc_val * w["val"]
                 + sc_sharpe * w["sharpe"] + sc_calmar * w["calmar"]
                 + sc_alpha * w["alpha"] + sc_dd * w["dd"] + sc_ir * w["ir"])

    return {"code": code, "name": f.get("name"), "type": f.get("type", ""),
            "nav": f.get("nav"), "navDate": f.get("navDate"),
            "r20": r20, "r60": r60, "r120": r120, "r250": r250,
            "vol": vol, "ann": ann, "mdd": mdd, "sharpe": sharpe, "z": z,
            "m": m, "s": s,
            "beta": beta, "alpha": alpha, "ir": ir, "calmar": calmar,
            "avr": avr, "dims": dims,
            "valPct": vp, "valBasis": val_basis,
            "managers": managers, "tenure": tenure,
            "sc": {"mom": sc_mom, "val": sc_val, "sharpe": sc_sharpe, "calmar": sc_calmar,
                   "alpha": sc_alpha, "dd": sc_dd, "ir": sc_ir, "avr": sc_avr},
            "composite": round(composite, 1)}


def _build_reasons(fac, verdict, cap, amount, capital, p1, p3, p6, p12):
    r = []
    r.append("类型：%s；近一年收益 %s，年化波动 %.1f%%，最大回撤 %s，夏普 %.2f，卡玛 %.2f。"
             % (fac["type"] or "—", _s(fac["r250"]), fac["vol"] * 100,
                _s(fac["mdd"]), fac["sharpe"], fac["calmar"]))
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


def advise_fund(code, capital=100000):
    fac = _compute_factors(code)
    if fac is None:
        return {"ok": False, "error": "未找到基金或历史净值不足 60 个交易日，无法稳健评估"}
    m, s = fac["m"], fac["s"]

    def mc(days, sims=2000):
        win_cnt = 0
        for _ in range(sims):
            g = 1.0
            for _ in range(days):
                g *= 1 + _norm_box(m, s)
            if g > 1:
                win_cnt += 1
        return win_cnt / sims

    p1, p3, p6, p12 = mc(21), mc(63), mc(126), mc(252)

    composite = fac["composite"]
    verdict = "推荐" if composite >= 68 else ("谨慎关注" if composite >= 50 else "暂不推荐")

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

    return {"ok": True, "code": code, "name": fac["name"], "type": fac["type"],
            "nav": fac["nav"], "navDate": fac["navDate"], "score": composite, "verdict": verdict,
            "cap": cap, "amount": amount, "capital": capital,
            "r20": fac["r20"], "r60": fac["r60"], "r120": fac["r120"], "r250": fac["r250"],
            "vol": fac["vol"], "ann": fac["ann"], "mdd": fac["mdd"], "sharpe": fac["sharpe"], "z": fac["z"],
            "beta": fac["beta"], "alpha": fac["alpha"], "ir": fac["ir"], "calmar": fac["calmar"],
            "avr": fac["avr"], "dims": fac["dims"],
            "valPct": fac["valPct"], "valBasis": fac["valBasis"],
            "managers": fac["managers"], "tenure": fac["tenure"],
            "p1": p1, "p3": p3, "p6": p6, "p12": p12, "reasons": reasons}


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
        if path == "/api/market":
            self._send(200, json.dumps(get_market_env(), ensure_ascii=False))
            return
        if path == "/api/recommend":
            # 动态全市场扫描较重，结果缓存 30 分钟（数据日频变化，足够动态且避免每次点击都大扫描拖垮服务）
            data = cached("reco", "dyn", 1800, recommend_portfolio)
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

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print("Fund Portfolio Analyzer running at http://localhost:%d" % PORT)
        httpd.serve_forever()
