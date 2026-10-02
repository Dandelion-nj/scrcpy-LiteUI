# scrcpy-LiteUI（快投）

![平台](https://img.shields.io/badge/平台-Windows%2010%20%7C%2011-0078D4)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![许可证](https://img.shields.io/badge/许可证-Apache--2.0-blue)
[![最新版本](https://img.shields.io/github/v/release/Dandelion-nj/scrcpy-LiteUI)](https://github.com/Dandelion-nj/scrcpy-LiteUI/releases)

Windows 上给 [scrcpy](https://github.com/Genymobile/scrcpy) 套的一层图形化启动器。
目标是把「安卓投屏」这件事从命令行里解放出来：**插上线或连上 Wi-Fi，点一下就能投**。

界面基于 pywebview（Edge WebView2）+ 原生 JS，无前端构建步骤；后端为单进程 Python，
可打包成**单文件便携 EXE**，拷到任何 Windows 机器直接运行。

## 快速开始

到 [Releases](https://github.com/Dandelion-nj/scrcpy-LiteUI/releases) 下载最新的 exe，
单文件便携版，无需安装，**双击即可运行**（`scrcpy`、`adb` 已随包附带）。

> 发布页里显示的名字是「快投.exe」，但 GitHub 的发布资产不支持中文文件名（会被平台清成
> `default.exe`），所以每个版本都以 `Kuaitou-v版本号.exe` 的名字上传
> （例如 v1.8.1 下载下来是 `Kuaitou-v1.8.1.exe`）。

| 项目 | 要求 |
| --- | --- |
| 系统 | Windows 10 1809+ / Windows 11 |
| 界面运行时 | **Microsoft Edge WebView2 Runtime**（多数 Win11 已内置；缺失时程序会提示并提供官方下载页） |
| 手机 | Android，需开启「开发者选项 → USB 调试」；扫码配对需 Android 11+ |

从源码运行、打包、发版见[开发](docs/开发.md)。

## 特性

**设备连接**

- 三级设备发现，逐级递进：局域网 5555 扫描 → mDNS 深度搜索 → 随机端口扫描
- 无线调试（Android 11+）自动发现、**扫码配对**
- **USB 一键转无线**：插着数据线时自动 `adb tcpip` + 取 Wi-Fi IP + 建立连接
- 一台设备一个主页（侧边栏「设备」分组），各有独立的应用列表与快捷启动
- **USB 有线优先**：同时连着有线设备时自动优先使用（延迟最低、最稳定）
- 掉线自动重连，退避 6s → 12s → 24s → 60s，最多 10 次；记住最近 5 台连接过的设备

**投屏**

- 镜像应用 / 镜像桌面两种模式
- 画面：编码格式（自动 / H.264 / H.265）、最大尺寸、帧率上限、码率
- 虚拟屏：自定义分辨率、UI 放大倍数、分辨率预设
- 可去除安卓状态栏与导航栏

**界面**

- 可折叠的左侧边栏，设置类标签收在底部；浅色（默认）/ 深色两套主题，主窗口标题栏跟主题同色
- 主页第一行与快捷启动区**固定在顶部**，不随列表滚动；快捷启动每台**最多 8 个**
- 界面图标一律用内联 SVG 绘制，不用 emoji（字形 / 颜色 / 尺寸跨系统一致，且自动跟随主题色）

**其它**

- 应用列表扫描 + 一键启动，内置图标匹配（多级回退，命中率高且不联网）
- **从手机取图标**：设备第一次连上就静默取回手机上真实在用的图标，优先于内置素材库显示
- 托盘常驻：关闭最小化到托盘，托盘右键可直接投屏 / 启动应用；支持开机自启动
- 单实例保护（不会开出第二份 adb）、屏幕解锁（后台静默输密码）、关闭物理屏幕（**不会锁屏**）
- 诊断面板：环境自检、一键诊断报告、日志导出

## 文档

| 我想…… | 看这里 |
| --- | --- |
| 学会怎么连手机、怎么投屏 | [使用教程](docs/使用教程.md) |
| 遇到问题排查 | [常见问题](docs/常见问题.md) |
| 自己改代码 / 重新打包 / 发版 | [开发](docs/开发.md) |

## 许可证

本项目代码以 **Apache-2.0** 授权，见 [LICENSE](LICENSE)。

随附的第三方组件（scrcpy、adb、FFmpeg、SDL、libusb）版权归各自所有者，
详见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
