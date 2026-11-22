# antigravity-geo-guard

修复 Antigravity 的 **`Agent execution terminated due to error.`**
——真正的原因是 Google 把你的**出口 IP 判定成 CN**，返回 `400 FAILED_PRECONDITION`，
而不是账号被封、也不是服务器抽风。

```bash
python3 geo_guard.py heal     # 切节点 → 强制后端重连 → 用日志验证
```

---

## 症状对照表

| 你看到的 | 真实含义 |
|---|---|
| 红色横幅 `Agent execution terminated due to error.` | 后端把**任意**错误统一包装成的一句话，不代表错误类型 |
| `Error ID: 485358ca-...-1510` | 就是 `trajectory_id + 步号`，不是工单号 |
| `400 {"status":"FAILED_PRECONDITION","message":"User location is not supported"}` | **地域拦截**，按出口 IP 判定 |
| 日志里 `403 PERMISSION_DENIED` + `Detected location: CN` | 同一件事的另一种表现 |
| 换了节点还是报错 | 长连接把旧链路钉死了，见下文 |

---

## 为什么「挂了 VPN 还是被判 CN」

三层叠加，缺一不可：

1. **出口 IP 的归属地 ≠ 看起来在美国**
   实测 `🇺🇸美国00|x2倍率 - 电信直连` 出口 `179.255.150.232`，
   `ip-api` 和 `ipinfo` **都**判 US，但 Google 判 **CN**。
   中转/落地型节点的落地 IP 被 Google 的 GeoIP 库归到了中国。
   > 所以拿第三方 GeoIP 判断"这个节点能不能用"是错的，只有 App 自己的日志算数。

2. **长连接不会跟着节点走**
   HTTP/2 连接在建立的一瞬间就把代理链路固定了（`chains: [节点, 节点选择]`）。
   你在 Clash 里切节点，只影响**新**连接；已经建立的那条还在走旧链路，
   于是「换了节点还是 CN」。
   > 这也是 Antigravity 官方自己的做法：`setProxy()` 之后必须 `closeAllConnections()`。

3. **空闲时根本不发请求**
   后端不活动时日志完全静止，`grep -c "Detected location: CN"` 天然是 0。
   「0 条错误」不等于修好了，只等于**还没发过请求**。

---

## 它怎么工作

```
detect  →  读 ~/Library/Logs/Antigravity/language_server.log 的新增部分
        命中 "Detected location: CN" / "LOCATION_NOT_ELIGIBLE" / FAILED_PRECONDITION
heal    →  切 mihomo 代理组的节点
        →  kill language_server（Electron 会在 ~20 秒内自动拉起，不用重启 App）
        →  等 settle 秒，看新日志
        →  clean 就记住这个节点；blocked 就换下一个
verify  →  「日志有新内容」+「0 条地域错误」同时成立才算通过
```

三个判定结果，不糊弄：

- `clean` —— 有新流量且没有地域错误（真的通过）
- `blocked` —— 有新流量且出现地域错误（真的没通过）
- `unverified` —— 一点新流量都没有（**无法判定**，需要你去 App 里点一次「重试」）

---

## 安装

只要 Python 3（macOS 自带），零第三方依赖。

```bash
git clone <this repo> && cd antigravity-geo-guard
python3 geo_guard.py doctor      # 先看看环境是否就绪
```

要求：

- Clash Verge Rev，且开启「服务模式」（提供 unix socket 控制接口）
- Antigravity 装在 `/Applications/Antigravity.app`

如果 socket 路径不同，在 `~/.config/antigravity-geo-guard/config.json` 里指定：

```json
{
  "group": "节点选择",
  "mihomo_socket": "/var/run/clash-verge-service/users/501/verge-mihomo.sock",
  "mihomo_api": "",
  "mihomo_secret": "",
  "candidates": ["🇺🇸美国02", "🇯🇵日本00 - 移动直连", "🇸🇬新加坡00"],
  "settle_seconds": 25
}
```

> 用 Clash 的 HTTP external controller 也可以：填 `mihomo_api`（如 `http://127.0.0.1:9097`）和 `mihomo_secret`。

---

## 命令

| 命令 | 作用 |
|---|---|
| `doctor` | 环境、后端进程、日志、代理组、当前节点一览 |
| `nodes` | 列出组内节点，标出当前节点和默认跳过的（中转/香港/台湾） |
| `status` | 只看判定：`clean` / `blocked` / `unverified` |
| `detect` | 扫描新增日志里的地域错误 |
| `heal` | 切节点 + 重启后端 + 复验（默认按候选顺序试 4 个） |
| `heal --node "🇺🇸美国02"` | 只针对某个节点修 |
| `probe` | 逐个试探候选节点，输出一张结果表（预检用） |
| `switch <node>` | 只切节点，不做其它 |
| `guard` | 常驻守护：发现地域错误自动 heal |
| `install-agent` | 装成 launchd 常驻项（开机自启） |
| `uninstall-agent` | 卸载 |

退出码：`0` 通过 / `1` 被拦截 / `2` 无法判定 / `3` 出错——方便塞进脚本。

### 典型用法

**出问题了，修一下：**

```bash
python3 geo_guard.py heal
# 然后去 Antigravity 里点一次「重试」
python3 geo_guard.py status      # 确认是 clean 而不是 unverified
```

**想一劳永逸：**

```bash
python3 geo_guard.py install-agent
# 之后每 60 秒检查一次，出现地域错误自动切节点并重启后端
tail -f ~/Library/Logs/antigravity-geo-guard.log
```

**先摸底哪个节点能用：**

```bash
python3 geo_guard.py probe --limit 4
```

---

## 边界（重要）

- 本工具治的是**出口 IP 被判成 CN**，治不了真正的账号处置。
  账号级问题在协议里是另一套状态码（`ADMIN_SUSPENDED`、`PARENT_SUSPENDED`、
  `LICENSE_REQUIRED`、`QUOTA_EXHAUSTED`、`SPEND_CAP_REACHED`、`CREDENTIALS_EXPIRED`），
  出现了请去看账号本身，不要指望换节点。
- 它不修改 Antigravity 的任何文件，不碰账号凭证，只做两件事：
  切换你的代理节点、重启那个会自动恢复的后端进程。
- 请遵守你所用服务的条款。反复用被判为 CN 的出口去请求，本身才是真正可能招来
  账号风险的行为——所以这个工具是在**降低**风险，而不是"绕过限制继续用"。

---

## 验证过的数据

修复后连续使用 12 小时（`02:19:50 → 14:56:51`，2689 行日志）：

| 指标 | 数值 |
|---|---|
| `streamGenerateContent` 调用 | 302 |
| `loadCodeAssist` | 131 |
| `fetchAvailableModels` | 129 |
| `"403 Forbidden"` / `"429` / `"401` / `"503` / `RESOURCE_EXHAUSTED` | **0** |
| `Detected location: CN` | **0** |

一个会给别人省时间的坑：这个日志**每一行**都以 `ERROR: logging before google.Init:` 开头
（glog 写 stderr 的怪癖），所以拿 `403`、`429` 这种裸数字 grep 全是假阳性
（时间戳里就有这些数字）。必须匹配 `"403 Forbidden"` 这类完整串。

---

## 几个吃过的亏（都已在代码里处理）

**1. 日志每行都以 `ERROR: logging before google.Init:` 开头**
glog 写 stderr 怪癖。所以 `grep 403`、`grep 429` 全是假阳性（时间戳里就有这些数字），
必须匹配 `"403 Forbidden"` 这种完整串。

**2. 从 `~/Downloads` 装成 launchd 常驻会静默卡死**
macOS 的 TCC 保护 `~/Downloads`。launchd 拉起的进程 `open()` 脚本文件时会等一个
永远不出现的授权弹窗——进程活着、CPU 0.0s、日志不更新。
所以 `install-agent` 会把脚本复制到
`~/Library/Application Support/antigravity-geo-guard/` 再挂上去。

**3. 机场会把「套餐剩余」塞进节点列表**
`nodes` / `heal` 会把这类非节点条目过滤掉，否则会去"切换到"一行文字。

**4. 「直连」航线默认跳过**
实测 `🇺🇸美国00|x2倍率 - 电信直连` 的出口 IP 被 Google 判 CN，
而 `🇺🇸美国02`（无中转标记）正常。所以带「直连/中转/香港/台湾」的排在候选末尾，
但不硬性排除——某些网络环境下直连反而是唯一能用的。

## 完整排障过程

见 [docs/故事线-从报错到根因.md](docs/故事线-从报错到根因.md)：
从「以为是封号」到「逆向确认是地域拦截」，包含四个反转点和四条走不通的探测路径。

## License

MIT
