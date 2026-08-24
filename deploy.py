#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键部署（绕开 Mac 代理对 git push 的拦截，改用 GitHub Contents API）。

线上真实拓扑（2026-08-20 确认）：
  - 前端：GitHub Pages，源仓库 Ivanxxxxxxxxx/fund-analyzer-web
          （https://ivanxxxxxxxxx.github.io/fund-analyzer-web/）
  - 后端 API：腾讯云 SCF（函数 URL），需另打包上传（见 deploy_scf.py --pack）
  - fund-analyzer（本目录所在仓库）仅作为后端 app.py 的源码源，非线上前端源

本脚本做两件事：
  1) 把 index.html / static/chart.umd.js 推到 fund-analyzer-web（Pages 前端，推完自动重建）
  2) 把 app.py 同步推到 fund-analyzer（后端源码源，便于追溯；SCF 后端仍需单独上传 zip）

用法（确保 GH_PAT 在环境变量里，或 macOS 钥匙串有 github.com 凭证）：
    cd /Users/ivan/WorkBuddy/2026-07-23-18-37-31/fund-analyzer
    python3 deploy.py
"""
import os, sys, base64, json, time, urllib.request, urllib.error, subprocess

FRONT_REPO = "Ivanxxxxxxxxx/fund-analyzer-web"   # Pages 前端源
BACK_REPO  = "Ivanxxxxxxxxx/fund-analyzer"        # 后端源码源（同时含 Render 用的前端）
FRONT_FILES = ["index.html", "static/chart.umd.js"]
BACK_FILES  = ["app.py", "index.html", "static/chart.umd.js", "deploy.py", ".gitignore"]

def resolve_token():
    tok = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok, "环境变量"
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-internet-password", "-s", "github.com", "-w"],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip(), "macOS 钥匙串"
        except Exception:
            pass
    return None, None

TOK, TOK_SRC = resolve_token()
if not TOK:
    print("✗ 未检测到 GitHub 令牌（GH_PAT 环境变量 与 macOS 钥匙串都没有）。")
    print("  方式一：在本环境运行前先 export GH_PAT=你的token")
    print("  方式二：在 Mac 终端解锁钥匙串后运行 python3 deploy.py")
    sys.exit(1)
else:
    print(f"· 使用令牌来源：{TOK_SRC}")

def api(repo, method, path, body=None, tries=3):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    for i in range(tries):
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {TOK}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Content-Type", "application/json")
        if body is not None:
            req.data = body
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("message", str(e))
            except Exception:
                msg = str(e)
            if e.code == 409 and method == "PUT":
                print(f"  · {path}: 409 冲突，重新拉取 sha 重试({i+1})")
                time.sleep(1)
                continue
            print(f"✗ {path}: HTTP {e.code} {msg}")
            return None
        except Exception as e:
            print(f"✗ {path}: 网络错误 {e}")
            return None
    return None

def push_file(repo, f, msg):
    print(f"→ 部署 {repo}/{f} ...")
    meta = api(repo, "GET", f)
    if meta is None:
        return
    sha = meta.get("sha")
    if sha is None:
        print(f"✗ {f}: 拿不到 sha（文件可能不存在或权限不足）")
        return
    data = open(f, "rb").read()
    content = base64.b64encode(data).decode()
    body = json.dumps({"message": msg, "sha": sha, "content": content}).encode()
    res = api(repo, "PUT", f, body)
    if res and res.get("content", {}).get("sha"):
        print(f"  ✓ {f} 已推送 (sha {res['content']['sha'][:10]})")
    else:
        print(f"  ✗ {f} 推送失败，请检查 token 权限（需 repo 写权限）")

print("\n[1/2] 前端 → Pages 仓库 %s" % FRONT_REPO)
for f in FRONT_FILES:
    push_file(FRONT_REPO, f, f"feat: 合并租售比与租买决策卡片·改用真实房价输入(移除区域均价估算)·修复持有成本乱码·稳定信号按真实走势 ({f})")

print("\n[2/2] 后端源码 → 仓库 %s" % BACK_REPO)
for f in BACK_FILES:
    push_file(BACK_REPO, f, f"chore: 前端租售比功能本次无后端变更 ({f})")

print("\n完成。")
print("· 前端：GitHub Pages 监听 fund-analyzer-web 会自动重建（几十秒~几分钟），刷新 https://ivanxxxxxxxxx.github.io/fund-analyzer-web/ 即可见。")
print("· 后端 API（SCF）：把腾讯云密钥写入 .env（TENCENT_SECRET_ID / TENCENT_SECRET_KEY）后，运行 `python3 deploy_scf.py` 即可一键打包并自动更新 SCF 函数代码（含新推荐字段 catReasons/同类排名等），无需手动上传。")
