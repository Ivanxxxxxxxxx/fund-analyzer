# 基金组合健康度分析 · 腾讯云 Lighthouse 部署指南

> 为什么用 Lighthouse：SCF 函数 URL 的默认域名被腾讯云在 2025-09-08 强制加了
> `Content-Disposition: attachment`（合规政策，应用层无法覆盖），导致浏览器把网页当附件下载。
> Lighthouse 是一台完整云服务器，没有这层网关，响应头完全由 `app.py` 控制，浏览器正常渲染。
> 且常驻不休眠，看盘更稳。服务器在国内，抓天天基金数据延迟低。

代码零第三方依赖（只用 Python 标准库），部署极简。

---

## 第一步：创建 Lighthouse 实例
1. 腾讯云控制台 → **轻量应用服务器** → **新建**。
2. **镜像**：选「系统镜像」→ **Ubuntu 22.04 LTS**（自带 python3，最省心）。
   - 别选「应用镜像」（WordPress/宝塔等会占端口、干扰）。
3. **地域**：广州/上海等离你近的。
4. **套餐**：最便宜的即可（2核2G 或 1核2G，个人看盘足够），按量或包月都行。
5. 设好登录密码（记住，后面 SSH 用），买下。
6. 实例列表里记下它的 **公网 IP**（形如 `1.2.3.4`）。

## 第二步：把代码传到服务器
在你**本机 Mac** 终端执行（替换 `1.2.3.4` 为你的公网 IP）：

```bash
# 在 fund-analyzer 项目目录的上级，打包（用 zip 最稳，避免 scp 目录权限问题）
cd /path/to/fund-analyzer
zip -r /tmp/fa.zip . -x "*.zip" "__pycache__/*"

# 传到服务器
scp /tmp/fa.zip root@1.2.3.4:/tmp/fa.zip

# SSH 进服务器，解压
ssh root@1.2.3.4
mkdir -p /opt/fund-analyzer && cd /opt/fund-analyzer
unzip -o /tmp/fa.zip
# 确认文件平铺：ls 应能看到 app.py index.html scf_bootstrap 等
```

> 如果你本机有 `git`，也可以直接在服务器 `apt install -y git && git clone <你的仓库> /opt/fund-analyzer`。

## 第三步：确认 Python 并快速验证
仍在服务器上：
```bash
python3 --version          # 应 >= 3.6
cd /opt/fund-analyzer
python3 app.py &           # 临时前台/后台起一下
curl -s -D - http://127.0.0.1:8000/ | head -8   # 应看到 HTTP/1.1 200 + text/html，且无 Content-Disposition
# 没问题就 Ctrl-C 或 kill 掉临时进程
```

## 第四步：设为常驻服务（开机自启 + 崩溃重启）
```bash
# 把 service 文件放到系统目录
cp /opt/fund-analyzer/fund-analyzer.service /etc/systemd/system/
# 若 python3 不在 /usr/bin/python3，先改路径：
#   which python3   -> 比如 /usr/local/bin/python3
#   sed -i 's#/usr/bin/python3#实际路径#' /etc/systemd/system/fund-analyzer.service
systemctl daemon-reload
systemctl enable fund-analyzer      # 开机自启
systemctl start fund-analyzer      # 立刻启动
systemctl status fund-analyzer     # 应显示 active (running)
journalctl -u fund-analyzer -n 20  # 看日志确认无报错
```

## 第五步：放行防火墙（关键！）
Lighthouse 的防火墙在**控制台单独管理**，必须放行端口，外网才能访问：
1. 控制台 → 轻量应用服务器 → 你的实例 → **防火墙** 标签页。
2. **添加规则** → 应用类型选「自定义」或直接填：
   - 协议：**TCP**
   - 端口：**8000**（若第四步改了 80，这里填 80）
   - 来源：**0.0.0.0/0**（允许所有，或填你自己的 IP 更私密）
3. 保存。

## 第六步：访问
浏览器打开：

```
http://1.2.3.4:8000/
```

应正常显示仪表盘（**没有下载问题**）。验证接口：`http://1.2.3.4:8000/api/fund/110011` 返回基金 JSON。

> 想免端口直接 `http://IP` 访问？把 `/etc/systemd/system/fund-analyzer.service` 里
> `Environment=PORT=8000` 改成 `Environment=PORT=80`，然后
> `systemctl daemon-reload && systemctl restart fund-analyzer`，防火墙放行 80 即可。

---

## 日常维护
- **更新代码**：本地改完 → `scp` / `git pull` 到 `/opt/fund-analyzer` → `systemctl restart fund-analyzer`。
- **看日志**：`journalctl -u fund-analyzer -f`。
- **停机/开机**：`systemctl stop/start fund-analyzer`。
- **费用**：Lighthouse 包月最低档通常几十元/月；若只是偶尔看，选按量计费更省。

## 与 SCF 方案的关系
`app.py` / `index.html` / `scf_bootstrap` 等文件在两种方案通用。`scf_bootstrap` 在 Lighthouse 上用不到
（那是 SCF Web 函数专用启动文件），忽略即可；`fund-analyzer.zip` 也可继续用于 SCF。
Lighthouse 方案额外用到 `fund-analyzer.service`（systemd 自启）。
