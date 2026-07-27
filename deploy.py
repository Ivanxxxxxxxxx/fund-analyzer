#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
腾讯云 SCF Web 函数 一键部署脚本（基金组合健康度分析工具）
============================================================
用途：自动在腾讯云创建 Web 函数 + 上传代码包 + 创建 API 网关触发，
      把公网访问网址打印出来。无需在控制台里点来点去。

前置条件（需你自己做，安全原因我无法代劳）：
  1. 在腾讯云控制台 -> 访问管理 -> API密钥管理 生成一组密钥
     https://console.cloud.tencent.com/cam/capi
  2. 把密钥填到本目录下的 .env 文件（脚本同目录）：
       TENCENT_SECRET_ID=你的SecretId
       TENCENT_SECRET_KEY=你的SecretKey
     （.env 只在你本机，不要提交/发给任何人）
  3. 本机安装 SDK：  pip install tencentcloud-sdk-python

运行：  python3 deploy.py
可选环境变量：  REGION=ap-guangzhou  FUNCTION_NAME=fund-analyzer
"""
import os
import sys
import base64
import json

# ---------- 0. 读取密钥（环境变量优先，其次 .env） ----------
def load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    cfg = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg

cfg = load_env()
SECRET_ID = os.environ.get("TENCENT_SECRET_ID") or cfg.get("TENCENT_SECRET_ID", "")
SECRET_KEY = os.environ.get("TENCENT_SECRET_KEY") or cfg.get("TENCENT_SECRET_KEY", "")
REGION = os.environ.get("REGION") or cfg.get("REGION") or "ap-guangzhou"
FUNCTION_NAME = os.environ.get("FUNCTION_NAME") or cfg.get("FUNCTION_NAME") or "fund-analyzer"
ZIP_NAME = "fund-analyzer.zip"

if not SECRET_ID or not SECRET_KEY:
    print("✗ 缺少腾讯云密钥。请在脚本同目录创建 .env 文件，内容：")
    print("    TENCENT_SECRET_ID=你的SecretId")
    print("    TENCENT_SECRET_KEY=你的SecretKey")
    print("  密钥获取：https://console.cloud.tencent.com/cam/capi")
    sys.exit(1)

# ---------- 1. 读 zip 并 base64 ----------
here = os.path.dirname(os.path.abspath(__file__))
zip_path = os.path.join(here, ZIP_NAME)
if not os.path.exists(zip_path):
    print(f"✗ 找不到 {ZIP_NAME}，请先在本目录生成部署包（本地执行：python3 -m zipfile -c {ZIP_NAME} * ）")
    sys.exit(1)

with open(zip_path, "rb") as f:
    zip_b64 = base64.b64encode(f.read()).decode("ascii")
print(f"✓ 已读取 {ZIP_NAME}（{len(zip_b64)} 字符 base64）")

# ---------- 2. 调用腾讯云 SDK ----------
try:
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.scf.v20180416 import scf_client, models
except ImportError:
    print("✗ 未安装 SDK，请先运行： pip install tencentcloud-sdk-python")
    sys.exit(1)

cred = credential.Credential(SECRET_ID, SECRET_KEY)
http_profile = HttpProfile()
http_profile.reqTimeout = 60
client_profile = ClientProfile()
client_profile.httpProfile = http_profile
client = scf_client.ScfClient(cred, REGION, client_profile)

# 2.1 创建 Web 函数（Type=HTTP 即 Web 函数）
print(f"→ 创建 Web 函数 {FUNCTION_NAME} (Region={REGION}, Runtime=Python3.9) ...")
create_req = models.CreateFunctionRequest()
create_req.FunctionName = FUNCTION_NAME
create_req.Runtime = "Python3.9"
create_req.Type = "HTTP"          # Web 函数（HTTP 函数级服务）
create_req.Handler = "scf_bootstrap"
create_req.Code = models.Code()
create_req.Code.ZipFile = zip_b64
create_req.Code.CodeSource = "ZipFile"
create_req.Description = "基金组合健康度分析工具"
try:
    client.CreateFunction(create_req)
    print("✓ 函数创建成功")
except Exception as e:
    msg = str(e)
    if "FunctionName exists" in msg or "已存在" in msg or "ResourceConflict" in msg:
        print("· 函数已存在，继续上传代码 / 更新触发")
    else:
        print("✗ 创建函数失败：", msg)
        sys.exit(1)

# 2.2 上传/更新代码包（幂等：已存在则覆盖）
print("→ 上传代码包 ...")
code_req = models.UpdateFunctionCodeRequest()
code_req.FunctionName = FUNCTION_NAME
code_req.Handler = "scf_bootstrap"
code_req.Code = models.Code()
code_req.Code.ZipFile = zip_b64
code_req.Code.CodeSource = "ZipFile"
try:
    client.UpdateFunctionCode(code_req)
    print("✓ 代码上传成功")
except Exception as e:
    print("✗ 代码上传失败：", str(e))
    sys.exit(1)

# 2.3 创建「函数 URL」触发（替代已于 2025-06-30 下线的 API 网关触发）
# 腾讯云 Web 函数自 2025-06-30 起停止新建 API 网关触发器，改用「函数 URL」获取公网地址。
print("→ 创建函数 URL（公网访问地址）...")
trigger_req = models.CreateTriggerRequest()
trigger_req.FunctionName = FUNCTION_NAME
trigger_req.TriggerName = f"{FUNCTION_NAME}-url"
trigger_req.Type = "http"          # 函数 URL 触发类型固定为 http（非 apigw）
trigger_req.Namespace = "default"
trigger_req.Enable = "OPEN"
trigger_req.TriggerDesc = json.dumps({
    "AuthType": "NONE",            # NONE=匿名开放访问；CAM=需鉴权
    "NetConfig": {"EnableIntranet": True, "EnableExtranet": True},  # 开启公网
})
try:
    resp = client.CreateTrigger(trigger_req)
    info = getattr(resp, "TriggerInfo", None)
    if info:
        print("  TriggerInfo:", str(info)[:600])
    print("✓ 函数 URL 已提交（网址见控制台：函数详情 -> 左侧「函数 URL」）")
except Exception as e:
    print("· 自动创建函数 URL 失败（", str(e)[:200], "）")
    print("  请到控制台手动创建：函数详情 -> 左侧「函数 URL」-> 新建函数 URL")
    print("  -> 授权类型选【开放】(匿名) -> 勾选公网访问 -> 确定，即可看到网址。")

# 2.4 提示访问路径
print("\n==================== 部署完成 ====================")
print(f"函数名   : {FUNCTION_NAME}")
print(f"地域     : {REGION}")
print("访问方式 : 函数详情 -> 左侧「函数 URL」-> 复制网址")
print("          （形如 https://xxxx.scm.tencentcs.com/ 或函数URL页所示）")
print("==================================================")
