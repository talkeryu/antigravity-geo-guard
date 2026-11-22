#!/usr/bin/env python3
"""antigravity-geo-guard — 修复 Antigravity 的 "User location is not supported" 地域拦截。

原理见 docs/故事线-从报错到根因.md，一句话版本：
  Google 按**出口 IP** 判定地域 → 被判 CN 就返回 400 FAILED_PRECONDITION；
  而 language_server 的 HTTP/2 长连接在建立时就把代理链路钉死了，
  所以「换节点」必须配合「强制重启后端进程」才会生效。

子命令：
  doctor    环境与状态一览
  nodes     列出当前代理组里的候选节点
  status    只看判定结果（不修复）
  detect    扫描日志里新增的地域错误
  heal      切节点 + 重启后端 + 复验（可指定节点）
  probe     逐个试探候选节点，找出可用的（heal 的预检版）
  guard     常驻守护：发现地域错误就自动 heal
  install-agent / uninstall-agent   安装/卸载 launchd 常驻项
"""

import argparse
import http.client
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime

APP_PATH = "/Applications/Antigravity.app"
LS_BIN = APP_PATH + "/Contents/Resources/bin/language_server"
LOG_PATH = os.path.expanduser("~/Library/Logs/Antigravity/language_server.log")
STATE_DIR = os.path.expanduser("~/.config/antigravity-geo-guard")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
GUARD_LOG = os.path.expanduser("~/Library/Logs/antigravity-geo-guard.log")
# 常驻副本：必须离开 ~/Downloads 等被 TCC 保护的目录，否则 launchd 拉起的进程
# 会在 open() 脚本文件时卡死等授权（表现为进程 CPU 0.0s、日志不更新）。
AGENT_DIR = os.path.expanduser("~/Library/Application Support/antigravity-geo-guard")
AGENT_SCRIPT = os.path.join(AGENT_DIR, "geo_guard.py")
PLIST_LABEL = "com.antigravity-geo-guard"
PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % PLIST_LABEL)

DEFAULT_GROUP = "节点选择"
# 这些落地被 Google 判成 CN 的概率很高，默认不参与自动轮换。
AVOID_PATTERNS = ["香港", "台湾", "中国", "直连", "中转", "relay", "回国", "CN2"]

# 真正的地域拒绝特征（注意：不能只匹配裸数字 403/429，
# 这个日志每行都以 "ERROR: logging before google.Init:" 开头，裸数字全是假阳性）
GEO_PATTERNS = [
    "Detected location: CN",
    "LOCATION_NOT_ELIGIBLE",
    '"status": "FAILED_PRECONDITION"',
]

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_UNVERIFIED = 2
EXIT_ERROR = 3


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    ensure_state_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = {
        "group": DEFAULT_GROUP,
        "candidates": [],          # 空 = 自动从代理组里挑
        "avoid": AVOID_PATTERNS,
        "settle_seconds": 25,      # 重启后端后等多久再判定
        "respawn_timeout": 45,     # 等后端自动拉起的最长时间
        "guard_interval": 60,
        "guard_cooldown": 180,     # 两次自动修复之间的最小间隔
        "mihomo_socket": "",       # 空 = 自动探测
        "mihomo_api": "",          # 比如 http://127.0.0.1:9097
        "mihomo_secret": "",
    }
    cfg.update(load_json(CONFIG_PATH, {}) or {})
    cfg["group"] = os.environ.get("GEO_GUARD_GROUP", cfg["group"])
    return cfg


def log_line(msg):
    line = "[%s] %s" % (now_str(), msg)
    # 被 launchd 拉起时 stdout 已重定向到同一个日志，双写会出现重复行
    if sys.stdout.isatty():
        print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(GUARD_LOG), exist_ok=True)
        with open(GUARD_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# mihomo（Clash Verge Rev）控制接口
# --------------------------------------------------------------------------

class MihomoError(Exception):
    pass


class Mihomo:
    """优先走 Clash Verge Rev 的 unix socket，其次走 HTTP external controller。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.sock_path = cfg.get("mihomo_socket") or self._find_socket()
        self.api = (cfg.get("mihomo_api") or "").rstrip("/")
        self.secret = cfg.get("mihomo_secret") or ""

    @staticmethod
    def _find_socket():
        uid = os.getuid()
        cands = [
            "/var/run/clash-verge-service/users/%d/verge-mihomo.sock" % uid,
            "/var/run/clash-verge-service/verge-mihomo.sock",
            "/tmp/clash-verge-mihomo.sock",
        ]
        for p in cands:
            if os.path.exists(p) and os.access(p, os.R_OK | os.W_OK):
                return p
        return ""

    def _conn(self):
        if self.api:
            u = urllib.parse.urlparse(self.api)
            return http.client.HTTPConnection(u.hostname, u.port or 9097, timeout=8)
        if not self.sock_path:
            raise MihomoError(
                "找不到 mihomo 控制接口。请确认 Clash Verge Rev 已开启「服务模式」，"
                "或在 config.json 里设置 mihomo_socket / mihomo_api。"
            )

        class _UnixConn(http.client.HTTPConnection):
            def __init__(self, path):
                super().__init__("localhost", timeout=8)
                self._path = path

            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(8)
                self.sock.connect(self._path)

        return _UnixConn(self.sock_path)

    def _request(self, method, path, body=None):
        conn = self._conn()
        headers = {}
        if self.secret:
            headers["Authorization"] = "Bearer " + self.secret
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
        except (OSError, http.client.HTTPException) as e:
            raise MihomoError("访问 mihomo 失败：%s" % e)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if resp.status >= 400:
            raise MihomoError("mihomo 返回 %d：%s" % (resp.status, raw[:200]))
        return raw

    def group(self, name):
        raw = self._request("GET", "/proxies/" + urllib.parse.quote(name))
        return json.loads(raw)

    def current_node(self, group=None):
        group = group or self.cfg["group"]
        try:
            return self.group(group).get("now")
        except MihomoError:
            return None

    def members(self, group=None):
        group = group or self.cfg["group"]
        return list(self.group(group).get("all") or [])

    def select(self, node, group=None):
        group = group or self.cfg["group"]
        self._request("PUT", "/proxies/" + urllib.parse.quote(group), {"name": node})
        got = self.current_node(group)
        if got != node:
            raise MihomoError("切换后当前节点是 %r，不是 %r" % (got, node))
        return got


# --------------------------------------------------------------------------
# language_server 进程
# --------------------------------------------------------------------------

def ls_pids():
    try:
        out = subprocess.run(
            ["pgrep", "-f", LS_BIN], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return set()
    return {int(x) for x in out.split() if x.strip().isdigit()}


def kill_ls():
    """杀掉后端。它会由 Electron 自动拉起；不重启整个 App，避免丢会话。"""
    pids = ls_pids()
    if not pids:
        return set()
    subprocess.run(["kill"] + [str(p) for p in pids], capture_output=True)
    return pids


def wait_respawn(old_pids, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        new = ls_pids() - old_pids
        if new:
            return new
    return set()


# --------------------------------------------------------------------------
# 日志扫描
# --------------------------------------------------------------------------

class LogScanner:
    """按 inode + offset 增量扫描；日志被后端重启重置时自动从头读。"""

    def __init__(self, path=LOG_PATH):
        self.path = path

    def _stat(self):
        try:
            st = os.stat(self.path)
            return st.st_ino, st.st_size, st.st_mtime
        except OSError:
            return None

    def read_new(self, state, reset=False):
        """返回 (新文本, 是否为空内容, 新的 state)。"""
        st = self._stat()
        if st is None:
            return "", True, state
        ino, size, mtime = st
        offset = 0 if (reset or state.get("inode") != ino or size < state.get("offset", 0)) \
            else state.get("offset", 0)
        with open(self.path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
            new_offset = f.tell()
        text = chunk.decode("utf-8", "replace")
        return text, text == "", {"inode": ino, "offset": new_offset}

    def scan_all(self):
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def last_line(self):
        text = self.scan_all()
        lines = [l for l in text.splitlines() if l.strip()]
        return lines[-1] if lines else ""


def count_geo_hits(text):
    hits = {}
    for pat in GEO_PATTERNS:
        n = text.count(pat)
        if n:
            hits[pat] = n
    return hits


def classify(hits, grew):
    if hits:
        return "blocked"
    if not grew:
        return "unverified"
    return "clean"


# --------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------

def cmd_doctor(args):
    cfg = load_config()
    mh = Mihomo(cfg)
    print("antigravity-geo-guard doctor")
    print("  time            :", now_str())
    print("  python          :", sys.version.split()[0])
    print("  app             :", "OK" if os.path.isdir(APP_PATH) else "缺失 " + APP_PATH)
    print("  language_server :", "OK" if os.path.exists(LS_BIN) else "缺失 " + LS_BIN)
    pids = ls_pids()
    print("  后端进程        :", ("PID " + ",".join(map(str, sorted(pids)))) if pids else "未运行")
    if os.path.exists(LOG_PATH):
        st = os.stat(LOG_PATH)
        print("  日志            : %s (%d 字节, 更新于 %s)"
              % (LOG_PATH, st.st_size, datetime.fromtimestamp(st.st_mtime).strftime("%H:%M:%S")))
        last = LogScanner().last_line()
        print("  最后一行        :", (last[:100] + "…") if len(last) > 100 else (last or "(空)"))
    else:
        print("  日志            : 不存在（App 还没启动过？）")
    try:
        print("  mihomo 接口     :", mh.api or mh.sock_path or "未找到")
        print("  代理组          : %s（当前：%s）" % (cfg["group"], mh.current_node()))
        members = mh.members()
        print("  组内节点数      :", len(members))
    except MihomoError as e:
        print("  mihomo 接口     :", "错误 —", e)
    print("  配置            :", CONFIG_PATH, "存在" if os.path.exists(CONFIG_PATH) else "（默认值）")
    print("  状态            :", STATE_PATH)
    return EXIT_OK


def cmd_nodes(args):
    cfg = load_config()
    mh = Mihomo(cfg)
    members = mh.members(args.group)
    cur = mh.current_node(args.group)
    avoid = cfg["avoid"]
    for n in members:
        if not _looks_like_node(n):      # 机场塞进来的「套餐剩余」之类
            continue
        flags = []
        if n == cur:
            flags.append("当前")
        if any(a in n for a in avoid):
            flags.append("默认跳过")
        print(("%-42s %s" % (n, " ".join(flags))).rstrip())
    return EXIT_OK


def cmd_switch(args):
    cfg = load_config()
    mh = Mihomo(cfg)
    print("切换 →", mh.select(args.node, args.group))
    return EXIT_OK


def cmd_status(args):
    cfg = load_config()
    mh = Mihomo(cfg)
    state = load_json(STATE_PATH, {})
    scanner = LogScanner()
    text, empty, state = scanner.read_new(state, reset=args.all)
    hits = count_geo_hits(text)
    verdict = classify(hits, grew=not empty)
    node = mh.current_node()
    print("节点      :", node)
    print("新增日志  :", "0 字节" if empty else "%d 字节" % len(text))
    print("地域错误  :", ("%d 次 %s" % (sum(hits.values()), hits)) if hits else "0")
    print("判定      :", {"blocked": "被地域拦截", "clean": "干净（有新流量且无错误）",
                         "unverified": "未验证（没有新流量，不代表已修好）"}[verdict])
    save_json(STATE_PATH, state)
    return {"blocked": EXIT_BLOCKED, "clean": EXIT_OK, "unverified": EXIT_UNVERIFIED}[verdict]


def cmd_detect(args):
    cfg = load_config()
    scanner = LogScanner()
    state = load_json(STATE_PATH, {})
    text, empty, new_state = scanner.read_new(state, reset=args.all)
    hits = count_geo_hits(text)
    grew = not empty
    verdict = classify(hits, grew)
    if hits:
        print("检测到地域拦截：")
        for pat, n in hits.items():
            print("  %-40s %d 次" % (pat, n))
        for line in text.splitlines():
            if any(p in line for p in GEO_PATTERNS):
                print("  例:", line[:200])
    else:
        print("未检测到地域错误" + ("" if grew else "（注意：没有新增日志，判定为未验证）"))
    save_json(STATE_PATH, new_state)
    return {"blocked": EXIT_BLOCKED, "clean": EXIT_OK, "unverified": EXIT_UNVERIFIED}[verdict]


def _restart_and_verify(cfg, wait_seconds):
    """杀掉后端 → 等它自动拉起 → 等 settle → 看新日志。"""
    old = kill_ls()
    if not old:
        print("  ! 后端当前没有在运行，无法通过重启来刷新连接")
        return "error", {}
    new = wait_respawn(old, cfg["respawn_timeout"])
    if not new:
        print("  ! 后端在 %ds 内没有被自动拉起；请在 Antigravity 里操作一下触发重启"
              % cfg["respawn_timeout"])
        return "error", {}
    print("  后端已重启：PID %s" % ",".join(map(str, sorted(new))))
    # 日志会被重置，从 0 开始读
    state = {"inode": 0, "offset": 0}
    time.sleep(wait_seconds)
    text, empty, state = LogScanner().read_new(state, reset=True)
    hits = count_geo_hits(text)
    verdict = classify(hits, grew=not empty)
    return verdict, {"hits": hits, "state": state}


INFO_WORDS = ["套餐", "流量", "到期", "剩余", "官网", "重置", "："]


def _looks_like_node(name):
    """机场有时会把「套餐剩余」之类塞进节点列表，过滤掉。"""
    return not any(w in name for w in INFO_WORDS)


def _penalty(name, avoid):
    return sum(1 for a in avoid if a in name)


def _candidates(cfg, mh):
    members = [n for n in mh.members() if _looks_like_node(n)]
    if cfg["candidates"]:
        picked = [c for c in cfg["candidates"] if c in members]
        return picked or list(cfg["candidates"])
    avoid = cfg["avoid"]
    # 稳定排序：没有「直连/中转/香港」等标记的排前面，但不排除（可能只有它们能用）
    return sorted(members, key=lambda n: _penalty(n, avoid))


def _rotate(list_, start_after):
    if start_after in list_:
        i = list_.index(start_after)
        return list_[i + 1:] + list_[:i + 1]
    return list_


def cmd_heal(args):
    cfg = load_config()
    mh = Mihomo(cfg)
    state = load_json(STATE_PATH, {})

    if args.node:
        plan = [args.node]
    else:
        cands = _candidates(cfg, mh)
        if not cands:
            print("没有可用候选节点，请先配置 config.json 的 candidates")
            return EXIT_ERROR
        plan = _rotate(cands, mh.current_node())
        last_ok = state.get("last_working_node")
        if last_ok in cands:                      # 上次成功的节点优先再试一次
            plan = [last_ok] + [n for n in plan if n != last_ok]

    if args.dry_run:
        print("dry-run，将依次尝试：")
        for n in plan[: args.max_try]:
            print("  -", n)
        return EXIT_OK

    unverified = 0
    for idx, node in enumerate(plan[: args.max_try], 1):
        print("[%d/%d] 切到 %s" % (idx, min(len(plan), args.max_try), node))
        try:
            mh.select(node)
        except MihomoError as e:
            print("  ! 切换失败：", e)
            continue
        verdict, info = _restart_and_verify(cfg, args.settle)
        if verdict == "clean":
            state["last_working_node"] = node
            state["last_heal_at"] = time.time()
            state.update(info.get("state") or {})
            save_json(STATE_PATH, state)
            print("  ✓ 通过：新连接无地域错误")
            print("\n完成。如果 Antigravity 里还挂着旧的报错横幅，点一次「重试」即可。")
            return EXIT_OK
        if verdict == "unverified":
            unverified += 1
            print("  ? 后端重启后没有产生任何请求，无法判定这个节点")
            continue
        if verdict == "error":
            print("  ! 无法通过重启后端来刷新连接（Antigravity 可能没在运行）")
            break
        print("  ✗ 仍被判定（%s）" % (info.get("hits") or "错误"))

    if unverified and unverified == min(len(plan), args.max_try):
        print("\n所有候选都「无法判定」——因为后端空闲时不发请求。")
        print("请在 Antigravity 里点一次「重试」或发一句话，然后执行：")
        print("  python3 %s status" % os.path.abspath(__file__))
        return EXIT_UNVERIFIED

    print("\n没有找到可用节点。建议手动换一个原生落地（美国/日本/新加坡/加拿大）后重试。")
    return EXIT_BLOCKED


def cmd_probe(args):
    cfg = load_config()
    mh = Mihomo(cfg)
    cands = args.nodes or _candidates(cfg, mh)
    if not cands:
        print("没有候选节点")
        return EXIT_ERROR
    if args.limit:
        cands = cands[: args.limit]
    results = []
    for node in cands:
        print("\n== %s ==" % node)
        if not args.dry_run:
            try:
                mh.select(node)
            except MihomoError as e:
                print("  切换失败：", e)
                results.append((node, "switch-error"))
                continue
        else:
            print("  (dry-run 只切节点，不重启后端)")
        if args.dry_run:
            results.append((node, "skipped"))
            continue
        verdict, _ = _restart_and_verify(cfg, args.settle)
        results.append((node, verdict))
    print("\n结果汇总：")
    for node, v in results:
        print("  %-42s %s" % (node, v))
    return EXIT_OK


def cmd_guard(args):
    cfg = load_config()
    interval = args.interval or cfg["guard_interval"]
    cooldown = cfg["guard_cooldown"]
    log_line("guard 启动：间隔 %ds，冷却 %ds" % (interval, cooldown))
    scanner = LogScanner()
    state = load_json(STATE_PATH, {})
    while True:
        try:
            text, empty, new_state = scanner.read_new(state)
            state.update(new_state)
            save_json(STATE_PATH, state)
            hits = count_geo_hits(text)
            if hits:
                last = state.get("last_heal_at", 0)
                if time.time() - last < cooldown:
                    log_line("检测到地域错误，但仍在冷却期，等待")
                else:
                    log_line("检测到地域错误 %s，开始自动修复" % hits)
                    rc = cmd_heal(argparse.Namespace(
                        node=None, settle=cfg["settle_seconds"],
                        max_try=args.max_try, dry_run=False))
                    log_line("自动修复结束，退出码 %d" % rc)
                    state = load_json(STATE_PATH, state)
                    # 修复完成后把游标推到文件末尾，避免下一轮把历史上的地域错误
                    # 重新数一遍、从而每轮都触发一次切换（死循环）。
                    try:
                        st = os.stat(LOG_PATH)
                        state.update({"inode": st.st_ino, "offset": st.st_size})
                    except OSError:
                        state.update({"inode": 0, "offset": 0})
                    save_json(STATE_PATH, state)
        except Exception as e:  # 守护进程不能死
            log_line("异常：%r" % (e,))
        if args.once:
            return EXIT_OK
        time.sleep(interval)


def cmd_install_agent(args):
    cfg = load_config()
    script = os.path.abspath(__file__)
    python = shutil.which("python3") or "/usr/bin/python3"
    # 复制到不受 TCC 保护的目录再交给 launchd
    os.makedirs(AGENT_DIR, exist_ok=True)
    if script != AGENT_SCRIPT:
        shutil.copy2(script, AGENT_SCRIPT)
    script = AGENT_SCRIPT
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    plist = {
        "Label": PLIST_LABEL,
        "ProgramArguments": [python, "-u", script, "guard", "--interval", str(cfg["guard_interval"])],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": GUARD_LOG,
        "StandardErrorPath": GUARD_LOG,
        "ProcessType": "Background",
    }
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "bootout", "gui/%d" % os.getuid(), PLIST_PATH],
                   capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), PLIST_PATH],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap 失败：", r.stderr.strip())
        print("可以手动执行： launchctl bootstrap gui/%d %s" % (os.getuid(), PLIST_PATH))
        return EXIT_ERROR
    print("已安装并启动：", PLIST_PATH)
    print("日志：", GUARD_LOG)
    return EXIT_OK


def cmd_uninstall_agent(args):
    subprocess.run(["launchctl", "bootout", "gui/%d" % os.getuid(), PLIST_PATH],
                   capture_output=True)
    if os.path.exists(PLIST_PATH):
        os.remove(PLIST_PATH)
    if os.path.isdir(AGENT_DIR):
        shutil.rmtree(AGENT_DIR, ignore_errors=True)
    print("已卸载")
    return EXIT_OK


# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="geo-guard",
        description="修复 Antigravity 的 'User location is not supported'（出口 IP 被判成 CN）",
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("doctor", help="环境与状态一览").set_defaults(func=cmd_doctor)

    sp = sub.add_parser("nodes", help="列出代理组里的节点")
    sp.add_argument("--group")
    sp.set_defaults(func=cmd_nodes)

    sp = sub.add_parser("switch", help="切换到指定节点")
    sp.add_argument("node")
    sp.add_argument("--group")
    sp.set_defaults(func=cmd_switch)

    sp = sub.add_parser("status", help="只看判定结果")
    sp.add_argument("--all", action="store_true", help="扫描整个日志而不是增量")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("detect", help="扫描新增日志里的地域错误")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_detect)

    sp = sub.add_parser("heal", help="切节点 + 重启后端 + 复验")
    sp.add_argument("--node", help="只试这个节点")
    sp.add_argument("--settle", type=int, default=None, help="重启后等待秒数")
    sp.add_argument("--max-try", type=int, default=4, help="最多试几个节点")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_heal)

    sp = sub.add_parser("probe", help="逐个试探候选节点")
    sp.add_argument("nodes", nargs="*", help="指定节点（默认按配置/代理组）")
    sp.add_argument("--settle", type=int, default=None)
    sp.add_argument("--limit", type=int, default=6)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("guard", help="常驻守护")
    sp.add_argument("--interval", type=int, default=None)
    sp.add_argument("--max-try", type=int, default=4)
    sp.add_argument("--once", action="store_true")
    sp.set_defaults(func=cmd_guard)

    sub.add_parser("install-agent", help="安装 launchd 常驻项").set_defaults(func=cmd_install_agent)
    sub.add_parser("uninstall-agent", help="卸载 launchd 常驻项").set_defaults(func=cmd_uninstall_agent)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_ERROR
    if getattr(args, "settle", None) is None and hasattr(args, "settle"):
        args.settle = load_config()["settle_seconds"]
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
