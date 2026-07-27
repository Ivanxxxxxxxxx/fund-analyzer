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
    # 兜底2：纯 6 位代码，用 fundgz 直接取名称（最稳，支付宝/天天共用代码）
    if not items and re.match(r"^\d{6}$", key):
        g = _fundgz(key)
        if g and g.get("name"):
            items.append({"code": key, "name": g["name"], "type": "", "pinyin": ""})
    return {"ok": True, "items": items[:20]}


def _fundgz(code):
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


def get_detail(code):
    """基金档案：名称/类型/最新净值+涨跌/资产配置/前十大持仓。"""
    res = {"ok": True, "code": code, "name": "", "type": "", "nav": None,
           "navDate": None, "changePct": None,
           "assetAllocation": [], "industry": [], "holdings": []}
    txt = None
    for host in ("https://fundf10.eastmoney.com", "https://fund.eastmoney.com"):
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
        h = get_holdings(code)
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
    return res


def get_fund(code):
    detail = get_detail(code)  # 名称/类型/净值/涨跌/资产配置/持仓（最稳来源）
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
            "holdings": detail.get("holdings") or []}


def get_history(code, days=365):
    result = []
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
        except Exception as e:
            return {"ok": False, "error": str(e), "data": _dedupe(result)}
    return {"ok": True, "data": _dedupe(result)[:days] if days else _dedupe(result)}


def _dedupe(rows):
    seen, uniq = set(), []
    for r in rows:
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        uniq.append(r)
    uniq.sort(key=lambda x: x["date"])
    return uniq


def get_holdings(code):
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
            import stat as _stat
            def _perm(p):
                try:
                    m = _stat.S_IMODE(os.stat(p).st_mode)
                    return oct(m) + (" (可执行)" if m & 0o111 else " (不可执行!)")
                except Exception as e:
                    return "不存在: %s" % e
            info = {
                "python_version": sys.version.split()[0],
                "cwd": os.getcwd(),
                "BASE": BASE,
                "PORT_env": os.environ.get("PORT"),
                "list_BASE": sorted(os.listdir(BASE)),
                "scf_bootstrap_perm": _perm(os.path.join(BASE, "scf_bootstrap")),
                "app.py_exists": os.path.exists(os.path.join(BASE, "app.py")),
                "index.html_exists": os.path.exists(os.path.join(BASE, "index.html")),
                "protocol_version": "HTTP/1.1",
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
