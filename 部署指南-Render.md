# 部署指南 · Render（GitHub 推送，永久公网网址）

> 路线目标：**真正脱离 Mac**（不用保持开机），GitHub 推送代码 → Render 自动构建部署 → 得到永久 `https://xxx.onrender.com` 网址，手机/任意设备直接打开。
> 适用：你有 GitHub 账号即可，免费。
> 已知：Render 服务器在**海外**（美/新），抓天天基金（国内）数据**偶有不稳**，多数时候能用，没腾讯云国内节点稳。若 Render 上数据频繁拉不到，可回退到 `start-tunnel.sh`（cloudflared 本机隧道）或腾讯云函数 URL + 自有域名。

---

## 第一步：在 Mac 终端把代码推到 GitHub

> 项目文件就在你 Mac 本地：`/Users/ivan/WorkBuddy/2026-07-23-18-37-31/fund-analyzer/`
> （本文件所在目录）。沙箱里的 git 不可用，所以下面这些命令请在**你自己 Mac 的终端**里执行。

```bash
# 1. 进入项目目录
cd /Users/ivan/WorkBuddy/2026-07-23-18-37-31/fund-analyzer

# 2. 初始化 git 并提交（.gitignore 已备好，会自动排除 __pycache__ 等）
git init
git add -A
git commit -m "基金组合健康度分析：初始版本"

# 3. 去 GitHub 网页新建一个空仓库（例如 fund-analyzer）
#    ⚠ 新建时不要勾选 "Add a README" / "Add .gitignore"（避免和本地冲突）
#    建好后复制仓库的 HTTPS 地址，形如 https://github.com/你的名/fund-analyzer.git

# 4. 关联并推送
git branch -M main
git remote add origin https://github.com/你的名/fund-analyzer.git
git push -u origin main
```

推送成功后在 GitHub 页面能看到 `app.py` / `index.html` / `render.yaml` 等文件。

---

## 第二步：Render 控制台一键部署

1. 打开 https://dashboard.render.com → 注册/登录（可用 GitHub 账号直接登录）。
2. 右上角 **「New +」→「Web Service」**。
3. **Connect a repository** → 授权 GitHub → 选中刚推的 `fund-analyzer` 仓库。
4. 配置：
   - **Name**：`fund-analyzer`（随意）
   - **Region**：选离你近的，如 **Singapore（新加坡）** 或 **Oregon（美西）**
   - **Branch**：`main`
   - **Runtime**：`Python 3`
   - **Build Command**：`pip install -r requirements.txt`（零依赖，秒过）
   - **Start Command**：`python app.py`
   - **Plan**：**Free**（免费）
   - 高级里确认 **Health Check Path** 为 `/`（仓库里的 `render.yaml` 已写好）
5. 点 **「Create Web Service」**。

> Render 会读取仓库里的 `render.yaml` 预填大部分配置；上面手动项只是兜底确认。

---

## 第三步：拿到网址并验证

- 部署需要 1–3 分钟，日志里出现 `Server started on port ...` 即成功。
- 完成后 Render 给出网址，形如：`https://fund-analyzer.onrender.com`
- 浏览器打开 → 应看到「基金组合健康度分析」仪表盘。
- 验证接口：`https://fund-analyzer.onrender.com/api/fund/110011` 应返回一段基金 JSON。

---

## 注意事项

- **冷启动**：Render 免费版在约 15 分钟无访问后会休眠，下次打开需等待几秒~几十秒"唤醒"。仪表盘内置「净值每 60 秒自动刷新」，只要页面开着就不会睡；关掉很久后再开第一次会慢一点，属正常。
- **海外抓数据**：如前所述，服务器在海外访问天天基金偶有延迟/失败。若某次「开始分析」拉不到历史，刷新或重试通常即可；持续拉不到就换 cloudflared 本机隧道方案。
- **环境变量**：`app.py` 自动读取 Render 注入的 `PORT`，无需任何配置。
- **更新代码**：以后改了 `app.py` / `index.html`，本地 `git commit` + `git push` 到 `main`，Render 会**自动重新部署**。
- **不再用的资源**：之前的腾讯云 SCF 函数（默认域名政策导致浏览器下载，用不了）可在 SCF 控制台删除；`start-tunnel.sh` 可保留作临时/备用。

---

## 故障排查

| 现象 | 原因/对策 |
|---|---|
| 部署失败，日志报 `python: not found` | 确认 Runtime 选了 `Python 3`，Start Command 写 `python app.py` |
| 打开网址显示 Render 默认页/404 | 代码未推到 `main` 分支，或 Start Command 错；检查 Render 的 Events 日志 |
| 仪表盘能开，但「开始分析」拉不到数据 | 海外节点访问 eastmoney 波动；重试，或改用 cloudflared 本机隧道 |
| 接口返回 500 | 看 Render 日志里 app.py 的报错；多半是某个 fund 代码抓不到，换代码试试 |
