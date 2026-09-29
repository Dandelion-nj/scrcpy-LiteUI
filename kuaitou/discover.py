"""设备发现：三级递进。

1) 局域网：ARP 邻居 + 各网段 /24 探 5555 —— 几秒出结果，覆盖经典 tcpip 模式
2) mDNS：adb 自带 mDNS 发现「无线调试」广播（Android 11+ 官方发现方式）
3) 端口扫描：对存活主机扫 30000~50000 并逐个 adb connect 复核

「深度搜索」的后台任务与进度状态机（供 HTTP 层轮询 / 推送）也在这里。
依赖：storage、device。
"""


import concurrent.futures
import os
import re
import selectors
import socket
import subprocess
import threading
import time
from collections import deque

from .storage import load_config
from .device import (
    CLASSIC_ADB_PORT, get_devices, get_startupinfo, device_info, device_states,
    run_adb, _ip_sort_key, _usable_ip, _remember_device, _skip_reconnect,
)

# 无线调试的设备发现：不再扫描 30000-49999 随机端口段（太慢，且扫描时 connect
# 会顺带连上多台设备导致后续 scrcpy 报 Multiple devices），改为只找 IP、只探 5555。
_IPV4_RE = re.compile(r'\d{1,3}(?:\.\d{1,3}){3}')
_PROBE_TIMEOUT = 0.35
_PROBE_WORKERS = 256              # 并发过高手机防火墙会丢弃 SYN，反而一个都扫不到
def _probe_port(ip, port, timeout=None):
    s = socket.socket()
    s.settimeout(timeout or _PROBE_TIMEOUT)
    try:
        return s.connect_ex((ip, port)) == 0
    except Exception:
        return False
    finally:
        s.close()
def _arp_table():
    """读 Windows ARP 邻居表，返回 (本机各网卡地址, 邻居主机地址)。

    只按 IPv4 字面量提取，不依赖系统语言；也不要求邻居有 MAC —— 虚拟网卡
    （UU Lanplay / Tailscale）的邻居条目常常没有 MAC，而手机正好可能挂在
    这些网段上。
    """
    try:
        r = subprocess.run(["arp", "-a"], capture_output=True, timeout=8,
                           encoding="utf-8", errors="replace",
                           startupinfo=get_startupinfo(), creationflags=0x08000000)
        out = (r.stdout or "") + (r.stderr or "")
    except Exception:
        return [], []
    local, hosts = [], []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        if "---" in s:                       # 形如 "Interface: 172.19.163.2 --- 0x2a"
            m = _IPV4_RE.search(s)
            if m and _usable_ip(m.group(0)) and m.group(0) not in local:
                local.append(m.group(0))
            continue
        m = _IPV4_RE.match(s)                # 邻居条目行首即 IP
        if m and _usable_ip(m.group(0)) and m.group(0) not in hosts:
            hosts.append(m.group(0))
    return local, hosts

def _local_prefixes():
    """要按 /24 扫一遍的网段前缀。

    一台电脑常有多张网卡（物理网卡 + UU Lanplay / Tailscale 之类的虚拟网卡），
    手机连在哪个网段上并不确定，所以把本机各网卡、ARP 邻居以及配置里上次连过的
    地址所在网段全部覆盖到。
    """
    local, hosts = _arp_table()
    ips = list(local) + list(hosts) + [str(load_config().get("ip", ""))]
    prefixes = []
    for ip in ips:
        if not ip or not _usable_ip(ip):
            continue
        p = ip.rsplit(".", 1)[0]
        if p not in prefixes:
            prefixes.append(p)
    return prefixes

def _probe_5555(ips):
    """并发探测一批 IP 的 5555 端口，返回开放的 IP 集合。"""
    if not ips:
        return set()
    hits = set()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as ex:
            for ip, ok in zip(ips, ex.map(lambda a: _probe_port(a, CLASSIC_ADB_PORT),
                                          ips, chunksize=8)):
                if ok:
                    hits.add(ip)
    except Exception:
        pass
    return hits

def discover_devices():
    """发现可连接的设备：已连接设备 + ARP 已知主机 + 同网段 /24，只探测 5555 端口。

    扫描阶段不做任何 adb connect，用户点选后才去连接，因此不会把第二台设备
    意外连上、也就不会再出现 'Multiple ADB devices'。
    """
    t0 = time.time()
    entries, order = {}, []

    def add(ip, source, connected=False, port=None, usb=False):
        if ip not in entries:
            entries[ip] = {"ip": ip, "port": str(port or CLASSIC_ADB_PORT),
                           "addr": "%s:%s" % (ip, port or CLASSIC_ADB_PORT),
                           "source": source, "connected": connected, "usb": usb}
            order.append(ip)
        else:
            if connected:
                entries[ip]["connected"] = True
            if usb:
                entries[ip]["usb"] = True
        return entries[ip]

    # 1) adb 已在线的设备：直接列出（USB 直连的序列号不带端口，无线的是 ip:port）
    for d in get_devices():
        info = device_info(d)
        usb = ":" not in d
        add(info["ip"], "USB 有线" if usb else "已连接",
            connected=True, port=info["port"] or CLASSIC_ADB_PORT, usb=usb)
        entries[info["ip"]]["addr"] = d

    # 2) ARP 邻居（标为"已知设备"），3) 各本机网段 /24 内的其余主机
    local_ips, arp = _arp_table()
    known = set(local_ips) | set(arp)
    for ip in arp:
        add(ip, "已知设备")
    for p in _local_prefixes():
        for h in range(1, 255):
            add("%s.%d" % (p, h), "同网段")

    candidates = [entries[i]["ip"] for i in order if not entries[i]["connected"]]
    open5555 = _probe_5555(candidates)

    found = []
    for ip in order:
        e = entries[ip]
        if e["connected"]:
            found.append(e)                      # 已连接的照常显示
        elif ip in open5555:
            e["source"] = "已知设备" if ip in known else "同网段"
            found.append(e)
    found.sort(key=lambda e: (not e["connected"], not e.get("usb"),
                              e["source"] != "已知设备", _ip_sort_key(e)))
    return {"found": found, "elapsed": round(time.time() - t0, 1)}

# ---------- 设备发现：mDNS ----------
# Android 11+ 打开无线调试后，手机会广播 _adb-tls-connect（可直连）和
# _adb-tls-pairing（等配对）两种服务。mDNS 走组播，能发现和电脑不同 /24 网段、
# 但在同一个广播域里的设备。
def mdns_available():
    """adb 的 mDNS 后端是否可用。打包内置的 adb 37 默认可用，老版本可能不行。"""
    out, err, rc = run_adb(["mdns", "check"], timeout=8)
    msg = ((out or "") + (err or "")).strip()
    return "daemon version" in msg, msg

def mdns_services(timeout=8):
    """解析 `adb mdns services`：返回 ([(实例名, 服务类型, IP, 端口)], 错误文本)。

    输出形如 `adb-XXXX-HgzRvA\t_adb-tls-connect._tcp\t192.168.1.39:42865`，
    也容错空白分隔；只收 `_adb-tls-*` 服务。
    """
    out, err, rc = run_adb(["mdns", "services"], timeout=timeout)
    rows = []
    for line in (out or "").splitlines():
        line = line.rstrip("\r").strip()
        if not line:
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 3 or not parts[1].strip().startswith("_adb-tls-"):
            continue
        instance, svc, endpoint = parts[0].strip(), parts[1].strip(), parts[2].strip()
        ip, _, port = endpoint.rpartition(":")
        if ip and port.isdigit():
            rows.append((instance, svc, ip, port))
    return rows, ((out or "") + (err or "")).strip()

def collect_mdns(window=5.0, interval=1.2):
    """在时间窗内反复查 `adb mdns services`，把中途出现的服务合并起来。

    mDNS 是异步发现的：adb 服务刚起来时列表常是空的，手机广播也要几秒才会进缓存，
    所以单次查询很容易"一个都搜不到"。按 interval 轮询整个 window，同一地址只留
    一条（优先可直连的 _adb-tls-connect）。
    """
    box, deadline = {}, time.time() + window
    while True:
        rows, _ = mdns_services()
        for instance, svc, ip, port in rows:
            addr = "%s:%s" % (ip, port)
            old = box.get(addr)
            if old is None or (old[1].startswith("_adb-tls-pairing")
                               and svc.startswith("_adb-tls-connect")):
                box[addr] = (instance, svc, ip, port)
        left = deadline - time.time()
        if left <= 0:
            break
        time.sleep(min(interval, left))
    return list(box.values())

# 深度搜索分三级，逐级变慢、逐级更全：
#   1) 局域网：ARP 邻居 + 各网段 /24 探 5555 —— 几秒出结果，覆盖经典 tcpip 模式
#   2) mDNS：adb 自带 mDNS 发现「无线调试」广播（Android 11+ 官方发现方式）
#   3) 端口扫描：对存活主机扫 30000~50000 —— 「无线调试」的随机端口就落在这个区间，
#      mDNS 被防火墙 / 路由器隔离挡住时，只有这一路能捞到设备
# 全程在后台线程跑，前端轮询进度，界面不会卡住。参数都设了上限，防止在大网络里失控。
_DEEP_PORT_LO = 30000
_DEEP_PORT_HI = 50000          # Android 11+ 无线调试随机端口区间（不含上界）
_DEEP_MAX_HOSTS = 12           # 单次最多扫多少台存活主机（12 × 9s ≈ 110s，留足预算余量）
_DEEP_BUDGET = 150.0           # 端口扫描总时间预算（秒），到点收工返回已找到的
_DEEP_PROBE_TIMEOUT = 0.12     # 局域网 RTT 极低，0.12s 足够；缩短能大幅提速
# 每条扫描线程同时挂起的连接数。两个硬约束：
#   1) Windows 的 select() 一次只能收 512 个句柄（超过就 ValueError: too many file
#      descriptors / OSError 10022），所以必须 <512；
#   2) 实测在飞连接超过约 800 个时，手机 / AP 会开始成片丢掉 SYN —— 扫得"越快"
#      反而一个 adb 端口都扫不到（3840 在飞时命中 0，640 在飞时稳定命中）。
# 80 × 8 线程 = 640 在飞，是实测既不丢包又够快的点。
_SCAN_WINDOW = 80
# 一趟扫描约 5% 的开放端口会被随机丢包漏掉（实测 20 趟漏 1 趟）；同一段扫两趟
# 几乎不漏（实测 20 趟 0 漏）。两趟的代价是耗时翻倍，所以主机数上限相应收紧。
_SCAN_PASSES = 2
_MDNS_WINDOW = 6.0
# 端口命中的复核：adb connect 打在"开放但不回话"的端口上要等约 10 秒，必须设上限。
# 经验上手机在这个区间最多开 1~2 个端口；普通电脑 / 路由器会一口气开十几个
# （Windows RPC、Steam 之类），用 _PORT_NOISE_MAX 这类"噪音主机"直接跳过复核。
_PORT_NOISE_MAX = 4            # 一段时间内开放端口超过这个数，就当普通电脑，不复核
_VERIFY_MAX_PER_HOST = 4       # 每台主机最多复核几个候选端口
_VERIFY_TIMEOUT = 3            # 单个候选端口的 adb connect 超时（秒）
_VERIFY_BUDGET = 25.0          # 整轮复核的总时间预算（秒）

def _deep_threads():
    """端口扫描线程数：跟着 CPU 走（线程数不固定），下限 4、上限 8。

    每个线程内部用非阻塞 connect + selectors，一次能挂几十个连接在飞，
    所以几线程就顶替"几百个阻塞线程"：既快，也不会把系统拖垮。
    上限必须压在 8：在飞连接 = 线程数 × _SCAN_WINDOW，再多手机就开始丢 SYN 了。
    """
    cpu = os.cpu_count() or 4
    return max(4, min(8, cpu // 2))

_deep_lock = threading.Lock()
_deep_state = {
    "running": False, "phase": "idle", "text": "", "progress": 0.0,
    "found": [], "elapsed": 0.0, "error": "", "hosts": 0, "scanned": 0,
    "mdns_count": 0, "lan_count": 0, "port_count": 0,
}

def _deep_publish(**kw):
    with _deep_lock:
        _deep_state.update(kw)

def deep_state():
    """给前端轮询用的进度快照。"""
    with _deep_lock:
        st = dict(_deep_state)
    st["found"] = list(st["found"])
    return st

def _merge_found(lan, mdns_rows):
    """把局域网结果和 mDNS 结果按地址合并去重（mDNS 补充实例名 / 待配对信息）。"""
    box = {}
    for e in lan.get("found", []):
        box[e["addr"]] = dict(e, pairing=False, instance="", port_scan=False)
    # 必须是"这个地址"真的在线才算已连接：同一 IP 的另一个随机端口并不代表它也连上了
    online = set(get_devices())
    for instance, svc, ip, port in mdns_rows:
        addr = "%s:%s" % (ip, port)
        pairing = "pairing" in svc
        item = {"ip": ip, "port": port, "addr": addr, "instance": instance,
                "pairing": pairing, "usb": False, "port_scan": False,
                "source": "待配对（mDNS）" if pairing else "深度搜索（mDNS）",
                "connected": (not pairing) and addr in online}
        old = box.get(addr)
        if old is None:
            box[addr] = item
        else:                       # 同一台设备：补上 mDNS 信息，保留原有的来源标记
            old["pairing"] = pairing
            old["instance"] = instance
            old["connected"] = old["connected"] or item["connected"]
            if not old.get("source", "").startswith(("USB", "已知设备", "已连接")):
                old["source"] = item["source"]
    return box

def _alive_hosts(exclude):
    """存活主机清单 = ARP 邻居表里的地址（本机各网卡地址除外）。

    局域网扫描阶段已经对每个 /24 都发过 TCP SYN，凡是有回应的主机（哪怕端口全关）
    都会进 ARP 表，所以扫完 5555 再读一次 ARP，就等于拿到了"活着的主机"清单。
    """
    local, neighbors = _arp_table()
    mine = set(local)
    hosts = []
    for ip in neighbors + local:
        if ip in mine or ip in exclude or ip in hosts:
            continue
        hosts.append(ip)
    return hosts, mine

def _scan_shard(ip, ports, deadline, out, lock):
    """一个扫描线程：非阻塞 connect + selectors，最多同时挂 _SCAN_WINDOW 个连接。

    关键是超时收尾：对方丢包/被防火墙过滤时，connect 永远不会返回事件，必须自己
    按 _DEEP_PROBE_TIMEOUT 把过期连接剔掉再补新的，否则窗口会卡死在同一批端口上。
    「先剔旧、再补新」的顺序不能反：反过来的话整批同时超时会让 live 瞬间空掉，
    扫描会误判成"扫完了"直接结束（手机对关闭端口大多不回 RST，走的正是超时这条路）。
    """
    sel = selectors.DefaultSelector()
    it = iter(ports)
    live, exp = {}, deque()          # live: socket->port；exp: (到期时间, socket) 按序
    timeout = _DEEP_PROBE_TIMEOUT
    exhausted = False
    try:
        while True:
            now = time.time()
            if now > deadline:
                break
            while exp and exp[0][0] <= now:      # 超时的按顺序剔掉
                _, s = exp.popleft()
                if s in live:
                    try:
                        sel.unregister(s)
                    except Exception:
                        pass
                    live.pop(s, None)
                    s.close()
            while not exhausted and len(live) < _SCAN_WINDOW:
                p = next(it, None)
                if p is None:
                    exhausted = True
                    break
                s = socket.socket()
                s.setblocking(False)
                try:
                    if s.connect_ex((ip, p)) == 0:   # 极快的主机会立刻连上
                        with lock:
                            out.append(p)
                        s.close()
                        continue
                except Exception:
                    s.close()
                    continue
                try:
                    sel.register(s, selectors.EVENT_WRITE, p)
                except Exception:
                    s.close()
                    continue
                live[s] = p
                exp.append((now + timeout, s))
            if exhausted and not live:
                break
            try:
                events = sel.select(timeout=min(timeout, max(0.005, deadline - now)))
            except Exception:
                events = []
            for key, _m in events:
                s = key.fileobj
                try:
                    if s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                        with lock:
                            out.append(key.data)
                except Exception:
                    pass
                try:
                    sel.unregister(s)
                except Exception:
                    pass
                live.pop(s, None)
                s.close()
    finally:
        for s in list(live):
            try:
                sel.unregister(s)
            except Exception:
                pass
            s.close()
        sel.close()

def _scan_ports(ip, ports, deadline):
    """扫一台主机的一段端口，返回开放的端口号列表。

    端口分片给几个线程，每片内部走非阻塞 selectors：在飞连接几十个就能顶替
    "几百个阻塞线程"，且线程只有个位数。同一段扫 _SCAN_PASSES 趟：SYN 在手机 /
    AP 侧有一定概率被随机丢掉，单趟实测约 5% 漏报，扫两趟基本不漏。
    """
    ports = list(ports)
    n = _deep_threads()
    found = set()
    for attempt in range(_SCAN_PASSES):
        if attempt and time.time() > deadline:
            break
        out, lock = [], threading.Lock()
        threads = [threading.Thread(target=_scan_shard,
                                    args=(ip, ports[i::n], deadline, out, lock), daemon=True)
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        found |= set(out)
    return sorted(found)

def _try_connect_addr(addr, timeout=_VERIFY_TIMEOUT):
    """端口命中后用 adb connect 复核，并以 adb devices 的最终状态为准。

    不能只看 adb connect 打印什么：只要 TCP 通它就回 "connected to …"，而且上一次
    失败的尝试会在 adb 里留下一条 offline 记录 —— 下一次就直接变成
    "already connected to …"，于是普通电脑的随机开放端口也被当成"已连接"。
    所以只有 adb devices 里真是 device 才算连上；unauthorized 是"真设备但手机上
    还没点允许"；其余一律 disconnect 摘掉，既不留垃圾条目，也保证下次扫描不会
    再看到那句误导的 "already connected"。
    """
    out, err, _ = run_adb(["connect", addr], timeout=timeout)
    text = ((out or "") + (err or "")).lower()
    if any(x in text for x in ("cannot", "failed", "refused", "timeout")):
        _drop_addr(addr)
        return None
    states = device_states()
    if addr in states["device"]:
        _skip_reconnect.discard(addr)
        _remember_device(addr)
        return "device"
    if addr in states["unauthorized"]:
        _remember_device(addr)
        return "unauthorized"
    _drop_addr(addr)                 # offline / 不在列表：不是能用的 adb 端点
    return None

def _drop_addr(addr):
    """摘掉一次失败的 connect 尝试，避免它在 adb 设备列表里留下 offline 条目。"""
    try:
        run_adb(["disconnect", addr], timeout=5)
    except Exception:
        pass

def _deep_job():
    t0 = time.time()
    mDNS_err = ""
    try:
        # 1) 局域网 + mDNS 并行：先尽快把"看得见"的设备报出来
        _deep_publish(phase="lan", text="正在搜索局域网设备 + mDNS …", progress=0.05)
        ok_mdns, msg = mdns_available()
        mdns_rows, lan = [], {"found": []}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_mdns = ex.submit(collect_mdns, _MDNS_WINDOW) if ok_mdns else None
            f_lan = ex.submit(discover_devices)
            try:
                lan = f_lan.result(timeout=60)
            except Exception as e:
                mDNS_err = "局域网扫描失败：%s" % e
            if f_mdns is not None:
                try:
                    mdns_rows = f_mdns.result(timeout=_MDNS_WINDOW + 15)
                except Exception:
                    mdns_rows = []
            else:
                mDNS_err = mDNS_err or "adb mDNS 不可用（%s）" % (msg or "无输出")

        box = _merge_found(lan, mdns_rows)
        mdns_n = sum(1 for e in box.values() if e.get("instance"))
        _deep_publish(found=list(box.values()), mdns_count=mdns_n,
                      lan_count=len(box) - mdns_n, progress=0.15,
                      elapsed=round(time.time() - t0, 1),
                      text="局域网找到 %d 个目标，开始端口扫描 …" % len(box))

        # 2) 端口扫描：对所有存活主机扫 30000~50000（已连接的设备跳过，它已经能用）
        already = {e["ip"] for e in box.values() if e.get("connected")}
        hosts, _mine = _alive_hosts(already)
        prefer = [e["ip"] for e in box.values()
                  if _usable_ip(e.get("ip") or "") and e["ip"] not in hosts]
        hosts = [ip for ip in prefer + hosts if _usable_ip(ip)][:_DEEP_MAX_HOSTS]
        deadline = t0 + _DEEP_BUDGET
        threads = _deep_threads()
        ports = range(_DEEP_PORT_LO, _DEEP_PORT_HI)
        verify_left = _VERIFY_BUDGET
        _deep_publish(phase="ports", hosts=len(hosts), scanned=0)
        for idx, ip in enumerate(hosts):
            if time.time() > deadline:
                break
            _deep_publish(
                progress=0.15 + 0.85 * idx / max(len(hosts), 1),
                text="端口扫描 %d/%d：%s（%d 线程）…" % (idx + 1, len(hosts), ip, threads))
            try:
                hits = _scan_ports(ip, ports, deadline)
            except Exception:
                hits = []
            # 开放端口一堆的主机基本是电脑 / 路由器（Windows RPC、Steam…），
            # 逐个 adb connect 复核又慢又没意义，直接跳过。
            if 0 < len(hits) <= _PORT_NOISE_MAX and verify_left > 0:
                _deep_publish(text="复核候选 %s（%d 个端口）…" % (ip, len(hits)))
                for p in hits[:_VERIFY_MAX_PER_HOST]:
                    if verify_left <= 0 or time.time() > deadline:
                        break
                    t_v = time.time()
                    addr = "%s:%d" % (ip, p)
                    state = _try_connect_addr(addr)
                    verify_left -= time.time() - t_v
                    if state:
                        # 复核通过才算真设备：device=可直接用；unauthorized=真端口，
                        # 但手机上还没点「允许调试」，标成待授权而不是已连接
                        box[addr] = {
                            "ip": ip, "port": str(p), "addr": addr,
                            "instance": "", "pairing": False, "usb": False,
                            "port_scan": True, "pending": state == "unauthorized",
                            "connected": state == "device",
                            "source": "深度搜索（端口扫描）"}
            _deep_publish(
                found=list(box.values()), scanned=idx + 1,
                port_count=sum(1 for x in box.values() if x.get("port_scan")),
                elapsed=round(time.time() - t0, 1))

        if not box and mDNS_err and "List of discovered" not in mDNS_err:
            _deep_publish(error=mDNS_err)
    except Exception as e:
        _deep_publish(error="深度搜索出错：%s" % e)
    finally:
        with _deep_lock:
            _deep_state["running"] = False
            _deep_state["phase"] = "done"
            _deep_state["progress"] = 1.0
            _deep_state["elapsed"] = round(time.time() - t0, 1)
            _deep_state["text"] = "深度搜索完成：共 %d 个目标（耗时 %ss）" % (
                len(_deep_state["found"]), _deep_state["elapsed"])

def deep_discover():
    """启动一次深度搜索；已在跑就直接返回当前进度。结果由前端轮询 deep_state() 取。"""
    with _deep_lock:
        if not _deep_state["running"]:
            _deep_state.update({"running": True, "phase": "lan", "progress": 0.0,
                                "text": "开始深度搜索 …", "found": [], "error": "",
                                "elapsed": 0.0, "hosts": 0, "scanned": 0,
                                "mdns_count": 0, "lan_count": 0, "port_count": 0})
            threading.Thread(target=_deep_job, name="deep-scan", daemon=True).start()
    return deep_state()
