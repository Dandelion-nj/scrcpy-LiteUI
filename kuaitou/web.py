"""HTTP 层。

本地 HTTP 服务的路由与请求处理，以及暴露给前端的原生「另存为」对话框 js_api。
依赖：其余全部业务模块。
"""


import http.server
import json
import os
import re
import secrets
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import webview

from .apps import _app_name, import_icons_from_device, launch_icon_tool, list_apps, request_icon
from .device import (
    CLASSIC_ADB_PORT,
    _device_model,
    _launch_result,
    _remember_device,
    _skip_reconnect,
    device_info,
    device_states,
    get_unlock_pin,
    launch_app,
    launch_desktop,
    run_adb,
    screen_locked,
    screen_off,
    set_unlock_pin,
    unlock_now,
    usb_to_wifi,
)
from .discover import deep_discover, deep_state, discover_devices
from .storage import APP_VERSION, RES_DIR, load_config, save_config
from .system import (
    _apply_autostart,
    _qr_lock,
    _qr_payload,
    _qr_snapshot,
    _qr_state,
    _qr_svg,
    _qr_worker,
    _tray_sync,
    collect_logs,
    get_window,
    run_diag,
)

_SSE_INTERVAL = 0.25     # SSE 推送时检查进度的间隔（秒）


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def stream_deep(self):
        """SSE：把深度搜索的进度实时推给前端，直到这一轮结束。

        这里只推不启动——启动仍走 /api/discover/deep。否则浏览器端 EventSource
        断线自动重连时，会顺带触发一轮新的扫描（重连 = 又一次全端口扫描，很糟）。
        """
        self.protocol_version = 'HTTP/1.1'    # SSE 按 1.1 语义更标准
        self.close_connection = True          # 不带 Content-Length：以关闭连接表示流结束
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        last = None
        try:
            self.wfile.write(b': connected\n\n')   # 先发一帧注释，前端据此确认通道已建立
            self.wfile.flush()
            while True:
                st = deep_state()
                if st != last:                # 状态没变不发重复帧
                    self.wfile.write(b'data: ' +
                                     json.dumps(st, ensure_ascii=False).encode() + b'\n\n')
                    self.wfile.flush()
                    last = st
                if not st["running"]:         # 新的一轮还没开始，或本轮已收尾
                    break
                time.sleep(_SSE_INTERVAL)
        except OSError:
            pass        # 前端关了窗口或已切回轮询：连接断开，安静收工

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            with open(os.path.join(RES_DIR, 'index.html'), 'r', encoding='utf-8') as f:
                self.wfile.write(f.read().encode())

        elif path == '/api/status':
            st = device_states()
            cfg = load_config()
            self.send_json({
                "version": APP_VERSION,
                # devices=可用设备；pending=手机还停在「允许调试」弹窗；offline=掉线但 adb 还挂着
                # usb=数据线直连（序列号不带端口），界面据此标成「USB 有线」并优先使用
                # locked/pin_set=该设备的锁屏状态与是否已存密码，顶栏据此显示解锁状态与按钮。
                # 密码本身不回传给界面：界面上只需要知道「有没有设」，不留多余副本。
                "devices": [dict(device_info(d), model=_device_model(d), connected=True,
                                 usb=(":" not in d),
                                 locked=screen_locked(d),
                                 pin_set=bool(get_unlock_pin(d)))
                            for d in st["device"]],
                "pending": [dict(device_info(d), model=None, usb=(":" not in d))
                            for d in st["unauthorized"]],
                "offline": [dict(device_info(d), model=None, usb=(":" not in d))
                            for d in (st["offline"] + st["other"])],
                "config": cfg,
            })

        elif path == '/api/apps':
            qs = urllib.parse.parse_qs(parsed.query)
            force = qs.get('force', ['0'])[0] == '1'
            serial = qs.get('device', [''])[0]
            apps = list_apps(serial, force=force)
            self.send_json({"apps": apps, "device": serial})

        elif path == '/api/discover':
            self.send_json(discover_devices())

        elif path == '/api/discover/deep':
            self.send_json(deep_discover())

        elif path == '/api/discover/deep/status':
            self.send_json(deep_state())

        elif path == '/api/discover/deep/stream':
            self.stream_deep()

        elif path == '/api/qr/status':
            self.send_json(_qr_snapshot())

        elif path == '/api/diag':
            qs = urllib.parse.parse_qs(parsed.query)
            self.send_json(run_diag(deep=qs.get('deep', ['0'])[0] == '1'))

        elif path == '/api/log':
            # 只把日志文本交给前端，保存位置由用户在弹出的「另存为」里自选
            self.send_json({"text": collect_logs()})

        elif path == '/api/launch':
            qs = urllib.parse.parse_qs(parsed.query)
            pkg = qs.get('pkg', [''])[0]
            serial = qs.get('device', [''])[0] or None
            if pkg:
                proc, focused = launch_app(pkg, serial=serial)
                if focused:
                    # 该应用已经在投屏：没有重复拉起 scrcpy，只把已有窗口唤到前台
                    self.send_json({"ok": True, "focused": True})
                else:
                    self.send_json(_launch_result(proc))
            else:
                self.send_json({"ok": False, "error": "no package"}, 400)

        elif path == '/api/icon':
            qs = urllib.parse.parse_qs(parsed.query)
            pkg = qs.get('pkg', [''])[0]
            if not re.fullmatch(r'[A-Za-z0-9_.]+', pkg):
                self.send_response(400)
                self.end_headers()
                return
            icon = request_icon(pkg, name=_app_name(pkg))
            if icon and os.path.exists(icon):
                ctype = {'.png': 'image/png', '.webp': 'image/webp',
                         '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}[os.path.splitext(icon)[1]]
                with open(icon, 'rb') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'private, max-age=86400')
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()

        elif path == '/api/desktop':
            qs = urllib.parse.parse_qs(parsed.query)
            serial = qs.get('device', [''])[0] or None
            self.send_json(_launch_result(launch_desktop(serial=serial)))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length).decode() or '{}')
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "invalid json"}, 400)
            return

        if path == '/api/connect':
            ip = body.get('ip', '')
            port = str(body.get('port', '') or CLASSIC_ADB_PORT)
            addr = "%s:%s" % (ip, port)
            out, err, _ = run_adb(["connect", addr], timeout=15)
            text = ((out or "") + (err or "")).lower()
            ok = "connected" in text and not any(x in text for x in ("cannot", "failed", "refused"))
            if not ok:
                # adb connect 报失败、但设备其实已经在线或正等授权时，也算连上：
                # 否则界面会出现"明明连上了却显示连接失败"的误报。
                st = device_states()
                ips = [device_info(d)["ip"] for d in (st["device"] + st["unauthorized"])]
                ok = ip in ips
            if ok:
                _skip_reconnect.discard(addr)
                save_config({"ip": ip, "port": port})
                _remember_device(addr)
            self.send_json({"success": ok, "serial": addr, "out": out, "err": err})

        elif path == '/api/disconnect':
            dev = body.get('device', '')
            if dev:
                _skip_reconnect.add(dev)     # 用户主动断开：自动重连不要再把它拉回来
                run_adb(["disconnect", dev], timeout=10)
            self.send_json({"ok": True})

        elif path == '/api/usb2wifi':
            self.send_json(usb_to_wifi())

        elif path == '/api/qr/start':
            name = "kuaitou-" + secrets.token_hex(4)
            password = "".join(secrets.choice(string.ascii_letters + string.digits)
                               for _ in range(12))
            payload = _qr_payload(name, password)
            with _qr_lock:
                _qr_state["tok"] += 1
                tok = _qr_state["tok"]
                _qr_state.update({"state": "starting", "message": "", "addr": "",
                                  "name": name, "password": password})
            threading.Thread(target=_qr_worker, args=(tok, name, password),
                             daemon=True).start()
            self.send_json({"ok": True, "name": name, "password": password,
                            "payload": payload, "svg": _qr_svg(payload)})

        elif path == '/api/qr/cancel':
            with _qr_lock:
                _qr_state["tok"] += 1
                _qr_state.update({"state": "idle", "message": ""})
            self.send_json({"ok": True})

        elif path == '/api/pair':
            ip = body.get('ip', '')
            port = body.get('port', '')
            code = body.get('code', '')
            addr = f"{ip}:{port}"
            out, err, _ = run_adb(["pair", addr, code], timeout=15)
            self.send_json({"out": out + err, "success": "Successfully paired" in out + err})

        elif path == '/api/unlock/pin':
            # 解锁密码单独走一条路由，不进 /api/config：它存在 EXE 数据流里、按手机归档，
            # 不写进 config.json，免得配置文件被随手发出去时把密码带出去。
            serial = body.get('serial', '')
            if not serial:
                self.send_json({"ok": False, "error": "no serial"}, 400)
                return
            ok = set_unlock_pin(serial, body.get('pin', ''))
            self.send_json({"ok": ok, "pin_set": bool(get_unlock_pin(serial))})

        elif path == '/api/unlock/run':
            # 顶栏「解锁」按钮：在后台点亮屏幕 + 上滑 + 输密码，结果由前端提示
            serial = body.get('serial', '')
            if not serial:
                self.send_json({"ok": False, "error": "no serial"}, 400)
                return
            self.send_json(unlock_now(serial))

        elif path == '/api/screen/off':
            # 顶栏「关闭物理屏幕」：Android 15+ 只关显示不锁屏；老系统退回电源键那条路
            serial = body.get('serial', '')
            if not serial:
                self.send_json({"ok": False, "error": "no serial"}, 400)
                return
            self.send_json(screen_off(serial))

        elif path == '/api/icon/tool':
            # 在手机上打开取图工具，用户在手机上导出图标后再点「从手机导入图标」
            self.send_json(launch_icon_tool(package=body.get('package', ''),
                                            serial=body.get('serial', '') or None))

        elif path == '/api/icon/import':
            # 把手机上导出的图标收回来入库（入库后优先于随包预置的素材库）
            self.send_json(import_icons_from_device(serial=body.get('serial', '') or None))

        elif path == '/api/config':
            save_config(body)
            if 'minimize_to_tray' in body or 'autostart' in body:
                _tray_sync()          # 开关一变就启停托盘图标
            resp = {"ok": True}
            if 'autostart' in body:
                resp["autostart"] = _apply_autostart(bool(body['autostart']))
            self.send_json(resp)

# ---------- 原生「另存为」对话框（pywebview js_api）----------
# 诊断报告与日志导出都走这里：由用户在弹出的原生对话框里自选保存位置，
# 而不是应用偷偷往某个固定目录写文件。js_api 的回调本身跑在独立线程上，
# 正好满足 pywebview「文件对话框不能占用 GUI 线程」的要求。
class Api:
    def save_text(self, default_name, content):
        if not get_window():
            return {"ok": False, "error": "窗口未就绪"}
        try:
            result = get_window().create_file_dialog(
                webview.FileDialog.SAVE, directory="", allow_multiple=False,
                save_filename=default_name or "快投_导出.txt",
                file_types=("文本文件 (*.txt)",))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not result:                       # 用户取消
            return {"canceled": True}
        path = result[0] if isinstance(result, (list, tuple)) else str(result)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content or "")
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}
