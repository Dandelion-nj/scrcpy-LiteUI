# scrcpy-LiteUI（快投）

Windows 上给 [scrcpy](https://github.com/Genymobile/scrcpy) 套的一层图形化启动器。
目标是把「安卓投屏」这件事从命令行里解放出来：**插上线或连上 Wi-Fi，点一下就能投**。

界面基于 pywebview（Edge WebView2）+ 原生 JS，无前端构建步骤；后端为单进程 Python，
可打包成**单文件便携 EXE**，拷到任何 Windows 机器直接运行。

## 特性

**设备连接**
- **三级设备发现**，逐级递进（见下文「设备发现」）
- 无线调试（Android 11+）自动发现、扫码配对
- **USB 一键转无线**：插着数据线时自动 `adb tcpip` + 取 Wi-Fi IP + 建立连接
- 多设备标签页，每台设备独立上下文
- **USB 有线优先**：同时连着有线设备时自动优先使用（延迟最低、最稳定）
- 掉线自动重连，退避策略 6s → 12s → 24s → 60s
- 记住最近 5 台连接过的设备

**投屏**
- 镜像应用 / 镜像桌面两种模式
- 画面：编码格式（自动 / H.264 / H.265）、最大尺寸、帧率上限、码率
- 虚拟屏：自定义分辨率、UI 放大倍数、预设保存
- 可去除安卓状态栏与导航栏
- 真实帧率记录（`--print-fps`），便于排查卡顿

**其它**
- 应用列表扫描 + 一键启动，内置图标匹配（多级回退：精确 → 双向前缀/后缀 → 名称别名）
- 托盘：关闭最小化到托盘，托盘右键可直接投屏 / 启动应用
- 开机自启动（默认关闭），启用后静默启动到托盘
- 单实例保护
- 诊断面板：环境自检、一键诊断报告、日志导出

## 环境要求

- Windows 10 1809+ / Windows 11
- **Microsoft Edge WebView2 Runtime**（界面依赖；多数 Win11 已内置，缺失时程序会提示下载）

## 直接使用

目前仓库尚未发布 Release。请按下方说明从源码打包，或自行获取打包产物。

打包后的 `快投.exe` 为单文件便携版，无需安装，双击即可运行。

## 从源码运行

需要 Python 3.12。

```powershell
py -3.12 -m venv .build\venv
.build\venv\Scripts\python.exe -m pip install pywebview pystray qrcode pillow pyaxmlparser
.build\venv\Scripts\python.exe launcher_server.py
```

**注意**：`icons/` 目录未包含在本仓库中（原因见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)）。
缺少它不影响运行，只是应用列表中的图标会走降级显示。如需完整图标效果，自行准备
以包名命名的 `.webp` 文件放入 `icons/` 即可。`scrcpy/` 目录已包含，无需另外下载。

## 打包

```powershell
.build\venv\Scripts\python.exe -m pip install pyinstaller
.build\venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --noconsole `
  --icon appicon.ico --name KuaitouBuild `
  --hidden-import pystray._win32 `
  --hidden-import qrcode --hidden-import qrcode.image.svg `
  --add-data "index.html;." --add-data "config.json;." --add-data "appicon.ico;." `
  --add-data "icons;icons" --add-data "scrcpy;scrcpy" `
  launcher_server.py
```

产物在 `dist\KuaitouBuild.exe`。打包参数同样记录在 `KuaitouBuild.spec` 中。

## 项目结构

```
launcher_server.py        后端主程序（HTTP 服务 + adb/scrcpy 封装 + 存储层 + 托盘）
index.html                前端界面（原生 JS，无构建）
config.json               默认配置
appicon.ico               应用图标
KuaitouBuild.spec         PyInstaller 打包配置
start_web.vbs             源码态启动脚本
scrcpy/                   随附的 scrcpy / adb 运行时（第三方，见声明）
icons/                    应用图标库（未入库）
```

## 设备发现

按代价从低到高逐级递进，前两级约 6 秒即可出结果：

1. **局域网扫描** —— 遍历本机 /24 网段，探测经典 5555 端口。
   快，但只覆盖同网段、且仅对固定 5555 端口的设备有效。
2. **mDNS 深度搜索** —— 通过 `_adb-tls-connect._tcp` 服务发现，能拿到
   Android 11+ 无线调试的随机端口，可跨网段。
3. **端口扫描** —— 对上面的存活主机（来自 ARP 表）扫描 30000~50000 随机端口，
   逐个用 `adb connect` 复核，找到即顺手连上。

第 3 级的工程设计要点：**并发并非越高越好**。实测在飞连接数超过约 800 时，
手机/AP 会成片丢弃 SYN，命中率反而降到 0；本项目取 8 线程 × 80 并发。
扫描器采用非阻塞 `connect` + `selectors` 事件循环，而非「线程池 + 阻塞 connect」，
线程数从数百降到个位数，且不会卡住界面。每台主机最多复核 4 个候选端口，
开放端口数超过 4 个的主机（典型的 PC / 路由器）直接跳过复核。

## 存储说明

程序生成的配置、日志、图标缓存默认写入 `快投.exe` 自身的 **NTFS 数据流（ADS）**，
使 EXE 保持单文件、目录不留散落文件；在非 NTFS 卷或写入受限时自动回退到
EXE 同目录的普通文件。

> ⚠️ 注意：**ADS 不随文件复制**。把 EXE 拷到 U 盘 / FAT32 分区 / 网络盘，
> 或用压缩包、聊天工具传输时，这些数据会丢失（表现为配置重置）。
> 需要连同配置一起迁移时，请从 EXE 同目录获取回退模式下生成的配置文件。

日志（`scrcpy_launch.log`、`apps_scan.log`）超过 1MB 会自动截断轮转。

## 许可证

本项目代码以 **Apache-2.0** 授权，见 [LICENSE](LICENSE)。

随附的第三方组件（scrcpy、adb、FFmpeg、SDL、libusb）版权归各自所有者，
详见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。