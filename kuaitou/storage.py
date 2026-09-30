"""路径常量与存储层。

程序数据落到哪儿（RES_DIR / DATA_DIR / EXE_PATH）、EXE 自身 NTFS 数据流（ADS）的
读写、配置读写与日志轮转，以及几个通用的文本日志小助手。

本模块是依赖链最底层：只依赖标准库，不 import 其它业务模块，供上层按需取用。
"""


import json
import os
import sys

# 版本号：显示在设置页底部和诊断报告里。exe 被拷到多台电脑排查问题时，
# 靠它一眼就能确认两边跑的是不是同一个版本。
APP_VERSION = "1.4.0"

# 路径解析：PyInstaller 打包后，随包资源解包到只读临时目录（sys._MEIPASS）；
# 用户数据不再落成散落文件，而是写进 exe 自身的 NTFS 数据流（见下方存储层）。
# 未打包（源码直跑）时，资源与数据都在项目根目录，即包目录（kuaitou/）的上一级——
# 拆分成包之前这段代码在根目录的 launcher_server.py 里，__file__ 本身就是根目录，
# 移进包内后必须多退一级，否则会错指到 kuaitou/ 里，导致找不到 scrcpy / index.html。
def _resolve_paths():
    """返回 (资源目录, 数据目录, 数据流宿主文件)。"""
    if getattr(sys, "frozen", False):
        return (sys._MEIPASS,
                os.path.dirname(sys.executable),
                os.path.abspath(sys.executable))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 源码态沿用拆分前的做法：数据流挂在项目根目录的入口脚本上，
    # 配置 / 缓存 / 日志继续藏在入口脚本的数据流里，项目里不落下散文件。
    return root, root, os.path.join(root, "launcher_server.py")

RES_DIR, DATA_DIR, EXE_PATH = _resolve_paths()

ADB_PATH = os.path.join(RES_DIR, "scrcpy", "adb.exe")
SCRCPY_PATH = os.path.join(RES_DIR, "scrcpy", "scrcpy.exe")
BUILTIN_CONFIG_FILE = os.path.join(RES_DIR, "config.json")     # 只读：内置默认配置
ICON_DIR = os.path.join(DATA_DIR, "icons")                     # 兜底：ADS 不可用时的图标目录
_RES_ICON_DIR = os.path.join(RES_DIR, "icons")                 # 只读：内置图标库
# 图标查找顺序：可写目录优先，其次内置图标库（未打包时两者相同，避免重复查找）
ICON_SEARCH_DIRS = ([ICON_DIR] if os.path.normcase(ICON_DIR) == os.path.normcase(_RES_ICON_DIR)
                    else [ICON_DIR, _RES_ICON_DIR])
ICON_EXTS = (".png", ".webp", ".jpg", ".jpeg")

# ---------- 存储层：EXE 自身的 NTFS 备用数据流（ADS）----------
# 便携目标：应用产生的一切文件（配置 / 应用缓存 / 投屏日志 / 抓取的图标 / 错误日志）
# 都写进「快投.exe」自己的数据流里，磁盘上不再散落任何小文件，拷走一个 exe 就带走全部状态。
# 所在卷不是 NTFS（FAT32 / 网络盘 / 无写权限）时 ADS 会失败，此时自动退回 exe 同目录的
# 普通文件，功能不受影响；当前实际存储位置会在诊断报告里说明。
# 实测注意：ADS 路径不支持 os.replace（WinError 87），因此一律直接覆盖写。

CONFIG_STREAM = "config.json"                 # 用户配置
APPS_CACHE_STREAM = "apps_cache.json"         # 应用列表缓存
LAUNCH_LOG_STREAM = "scrcpy_launch.log"       # scrcpy 启动/输出日志
SCAN_LOG_STREAM = "apps_scan.log"             # 应用扫描异常日志
ERROR_LOG_STREAM = "快投_错误日志.txt"          # 启动致命错误日志
ICON_INDEX_STREAM = "icon_index.json"         # 已缓存图标的包名索引
ICON_STREAM_PREFIX = "icon_"                  # 图标流：icon_<包名>.webp
UNLOCK_PINS_STREAM = "unlock_pins.json"       # 锁屏解锁密码：{设备键: 混淆串}，按手机归档（不进 config.json）

_ads_ok = None                                # None=尚未探测；True/False=探测结果

def ads_path(stream):
    return "%s:%s" % (EXE_PATH, stream)

def _ads_usable():
    """宿主文件所在卷是否支持 ADS。不用 fsutil（需要管理员），直接一次读写试错。"""
    global _ads_ok
    if _ads_ok is None:
        probe = ads_path(".ads_probe")
        try:
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            _ads_ok = True
            try:
                os.remove(probe)
            except OSError:
                pass
        except Exception:
            _ads_ok = False
    return _ads_ok

def storage_open(path, binary=False, append=False):
    if binary:
        return open(path, "ab" if append else "wb")
    return open(path, "a" if append else "w", encoding="utf-8", errors="replace")

def _fallback_path(stream):
    p = os.path.join(DATA_DIR, stream)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return p

# 单个日志流上限：日志藏在 exe 的数据流里，用户看不见也没法手动清理，
# 长期用会一直涨。排查问题只需要最近一次投屏的记录，所以超限就整个丢弃重来。
MAX_LOG_BYTES = 1024 * 1024

def _rotate_log(path):
    try:
        if os.path.getsize(path) > MAX_LOG_BYTES:
            os.remove(path)
    except OSError:
        pass

def storage_write(stream, data, binary=False, append=False):
    """写入：优先 EXE 数据流，不支持时退回 exe 同目录普通文件。返回 (实际路径, 是否ADS)。"""
    if _ads_usable():
        p = ads_path(stream)
        try:
            with storage_open(p, binary, append) as f:
                f.write(data)
            return p, True
        except Exception:
            pass
    try:
        p = _fallback_path(stream)
        with storage_open(p, binary, append) as f:
            f.write(data)
        return p, False
    except Exception:
        return "", False

def storage_read(stream, binary=False):
    """读取：先试 EXE 数据流，再试 exe 同目录普通文件（兼容旧版本遗留的文件）。"""
    cands = [ads_path(stream)] if _ads_usable() else []
    cands.append(os.path.join(DATA_DIR, stream))
    for p in cands:
        try:
            if binary:
                with open(p, "rb") as f:
                    return f.read()
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            continue
    return None

def storage_delete(stream):
    """删掉一个流（数据流与回退文件都试一遍）。用于「清空某项配置」：文件不存在就当删过了。"""
    cands = ([ads_path(stream)] if _ads_usable() else []) + [os.path.join(DATA_DIR, stream)]
    for p in cands:
        try:
            os.remove(p)
        except OSError:
            pass

def storage_open_append(stream, binary=False):
    """以追加方式打开一个长期持有的句柄（供子进程 stdout 重定向）。失败返回 None。"""
    if _ads_usable():
        path = ads_path(stream)
        _rotate_log(path)
        try:
            return storage_open(path, binary, True)
        except Exception:
            pass
    path = _fallback_path(stream)
    _rotate_log(path)
    try:
        return storage_open(path, binary, True)
    except Exception:
        return None

def storage_location():
    """诊断用：当前实际存储位置一览。"""
    kind = ("EXE 数据流（NTFS ADS）" if _ads_usable()
            else "exe 同目录普通文件（所在卷不支持 ADS 或不可写）")
    lines = ["宿主文件: %s" % EXE_PATH,
             "数据流可用: %s" % ("是" if _ads_usable() else "否"),
             "实际存储方式: %s" % kind]
    for stream in (CONFIG_STREAM, APPS_CACHE_STREAM, LAUNCH_LOG_STREAM,
                   ICON_INDEX_STREAM, SCAN_LOG_STREAM, ERROR_LOG_STREAM):
        p = ads_path(stream)
        try:
            n = os.path.getsize(p)
            lines.append("  [ADS] %-22s %d 字节" % (stream, n))
        except Exception:
            fp = os.path.join(DATA_DIR, stream)
            if os.path.exists(fp):
                lines.append("  [文件] %-21s %d 字节  %s"
                             % (stream, os.path.getsize(fp), fp))
            else:
                lines.append("  [无]   %s" % stream)
    return "\n".join(lines)

DEFAULT_CONFIG = {
    "ip": "",                   # 首次运行不带任何设备地址，由用户扫描 / 手动连接后再记住
    "port": "5555",
    "res_w": "1080",
    "res_h": "2400",
    "res_presets": ["720x1280", "1080x2400", "1440x3200"],
    "bitrate": "8",
    "scale": "1.0",
    "fps": "60",
    "audio_mode": "both",
    "video_codec": "auto",      # auto=交给 scrcpy；也可固定 h264 / h265
    "max_size": "0",            # 画面最大边长（像素），0 = 不限（原始分辨率）
    "reconnect_enabled": True,  # 无线掉线后后台自动重连（退避 + 次数上限）
    "autostart": False,         # 开机自启：静默启动，只驻托盘
    "recent_devices": [],       # 最近连接过的设备 [{addr, ip, port, name, ts}]，最多 5 台
    "quick_launch": {},         # {设备序列号: [包名...]}；"*" 为无专属列表时的默认值
    "minimize_to_tray": False,  # 开启后点关闭不退出，而是收进系统托盘继续待命
    # 图标从手机取回：手机上取图工具的包名（可留空，留空就只用「从手机导入」），
    # 以及它把图标导出到的目录；导入的图标优先于随包预置的素材库
    "icon_tool_package": "",
    "icon_import_dir": "/sdcard/Download/icons"
}

def load_config():
    merged = DEFAULT_CONFIG.copy()
    # 先读用户配置（EXE 数据流，或 ADS 不可用时的 exe 同目录文件）；
    # 缺失或不合法时回落到随包内置的默认配置
    raw = storage_read(CONFIG_STREAM)
    if raw:
        try:
            merged.update(json.loads(raw))
        except Exception:
            pass
    else:
        try:
            if os.path.exists(BUILTIN_CONFIG_FILE):
                with open(BUILTIN_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    merged.update(json.load(f))
        except Exception:
            pass
    # 旧版快捷启动是一个列表（全局共用），统一成 {设备: [...]} 结构，用 "*" 兜底
    ql = merged.get("quick_launch")
    if isinstance(ql, list):
        merged["quick_launch"] = {"*": ql} if ql else {}
    elif not isinstance(ql, dict):
        merged["quick_launch"] = {}
    return merged

def save_config(cfg):
    try:
        current = load_config()
        current.update(cfg)
        storage_write(CONFIG_STREAM,
                      json.dumps(current, ensure_ascii=False, indent=2))
    except Exception:
        pass

def _write_text(stream, text):
    """文本类小文件（错误日志 / 扫描日志）：优先写进 EXE 数据流，返回实际路径。"""
    path, _ = storage_write(stream, text)
    return path

def _read_log_tail(stream, limit=1200):
    return (storage_read(stream) or "")[-limit:]
