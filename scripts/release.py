"""一键发版：校验版本号 → 打包 → 校验产物 → 打标签 → 建 GitHub Release → 上传 exe。

用法：

    .build\\venv\\Scripts\\python.exe scripts\\release.py v1.9.0                  # 正式发一版
    .build\\venv\\Scripts\\python.exe scripts\\release.py v1.9.0 --dry-run        # 干跑，只看会发什么
    .build\\venv\\Scripts\\python.exe scripts\\release.py v1.9.0 --no-build       # 用已有产物发
    .build\\venv\\Scripts\\python.exe scripts\\release.py --update-repo-meta      # 只修仓库描述与 topics

发布说明放在 `release_notes/<tag>.md`（入库，跟代码一起 review），也可以用 --notes 指别处。

凭据：优先环境变量 GITHUB_TOKEN / GH_TOKEN，否则走本机已存的 git 凭据（git credential fill）。
网络全用标准库 urllib，不引第三方依赖。

发版是不可逆动作（要推标签、公开发布页），所以先用 --dry-run 空跑一遍。
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kuaitou.storage import APP_VERSION  # noqa: E402

DEFAULT_EXE = os.path.join("dist", "快投.exe")
NOTES_DIR = os.path.join(ROOT, "release_notes")
TARGET_BRANCH = "main"

# 仓库门面（GitHub 描述限 350 字符，topics 限 20 个、小写、连字符）
REPO_DESCRIPTION = ("Windows 下 scrcpy 的图形化启动器：插上数据线或连上 Wi-Fi，点一下就能投屏。"
                    "侧边栏设备入口、扫码配对、USB 一键转无线、单文件便携 EXE 无需安装。")
REPO_TOPICS = ["scrcpy", "screen-mirroring", "android", "adb", "windows", "python",
               "pywebview", "gui", "launcher", "pyinstaller"]

# CI 的 Windows runner 上 stdout 是管道，编码会取系统 ANSI 代码页，直接 print 中文会抛
# UnicodeEncodeError。这里统一钉成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


class ReleaseError(Exception):
    """发版过程中的可预期失败：打印一句话就退出，不甩堆栈。"""


class GitHubError(ReleaseError):
    def __init__(self, code, detail):
        super().__init__("GitHub 返回 %s：%s" % (code, detail))
        self.code = code


# ---------- 小工具 ----------

def _run(cmd, cwd=None, ok_codes=(0,)):
    """跑一条命令，实时把输出透给用户（打包要好几分钟，闷着不吭声太难受）。"""
    r = subprocess.run(cmd, cwd=cwd)
    if r.returncode not in ok_codes:
        raise ReleaseError("命令失败（退出码 %s）：%s" % (r.returncode, " ".join(cmd)))
    return r.returncode


def _git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise ReleaseError("git %s 失败：%s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r


def _read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _remote_repo():
    """从 origin 远端地址里解析出 owner/repo，省得把仓库名写死在脚本里。"""
    url = _git("remote", "get-url", "origin").stdout.strip()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if url.startswith(prefix):
            slug = url[len(prefix):]
            break
    else:
        raise ReleaseError("认不出 origin 的地址：%s" % url)
    slug = slug.removesuffix(".git").strip("/")
    if slug.count("/") != 1:
        raise ReleaseError("origin 地址里没解析出 owner/repo：%s" % url)
    return slug


def _token():
    """优先环境变量，其次本机已存的 git 凭据。

    这里的 stdin 交给 subprocess 直接喂：之前在 PowerShell 里走管道时每行会被加上 CR，
    git 会把 CR 当成值的一部分，报 "refusing to work with credential missing protocol field"。
    从 Python 传就没有这个问题。
    """
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()
    r = subprocess.run(["git", "credential", "fill"], cwd=ROOT,
                       input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise ReleaseError("取本机 git 凭据失败：%s（可以改用环境变量 GITHUB_TOKEN）"
                           % (r.stderr or "").strip())
    for line in r.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    raise ReleaseError("本机 git 凭据里没有 password 字段，可以改用环境变量 GITHUB_TOKEN")


def _api(method, url, token, payload=None, raw_path=None):
    """调一次 GitHub REST API，返回解析后的 JSON（空的就返回 {}）。"""
    data, ctype = None, None
    if raw_path is not None:
        data = open(raw_path, "rb")
        ctype = "application/octet-stream"
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
        ctype = "application/json; charset=utf-8"

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "token %s" % token)
    req.add_header("User-Agent", "kuaitou-release")
    req.add_header("Accept", "application/vnd.github+json")
    if ctype:
        req.add_header("Content-Type", ctype)
    if raw_path is not None:
        # 显式给 Content-Length：大文件走分块传输容易被上传接口拒掉
        req.add_header("Content-Length", str(os.path.getsize(raw_path)))

    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise GitHubError(e.code, e.read().decode("utf-8", "replace")) from e
    except urllib.error.URLError as e:
        raise ReleaseError("连不上 GitHub：%r" % (e.reason,)) from e
    finally:
        if hasattr(data, "close"):
            data.close()
    return json.loads(body) if body else {}


# ---------- 各步骤 ----------

def _check_version(tag):
    """标签上的版本号必须和源码里的一致，这是最容易犯的发布事故。"""
    ver = tag[1:] if tag.startswith("v") else tag
    if ver != APP_VERSION:
        raise ReleaseError("标签 %s 与源码版本 %s 对不上："
                           "先把 kuaitou/storage.py 的 APP_VERSION 改成 %s 再发" % (tag, APP_VERSION, ver))
    return ver


def _check_clean_tree():
    dirty = _git("status", "--porcelain").stdout.strip()
    if dirty:
        raise ReleaseError("工作区不干净，先提交或收拾掉这些改动再发版：\n%s" % dirty)


def _build(exe):
    if not os.path.exists(os.path.join(ROOT, "icons")):
        print("注意: icons/ 不在，本次产物的内置图标素材库为空——"
              "想要内置图标先跑 scripts/fetch_icons.py 再打包。")
    script = os.path.join(ROOT, "scripts", "check_exe_version.py")
    print("=== 打包 ===")
    _run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "KuaitouBuild.spec"], cwd=ROOT)
    print("=== 校验产物版本号 ===")
    _run([sys.executable, script, exe])


def _push_branch(dry_run):
    """打标签之前先把当前分支推上去。

    只推标签是不够的：标签指向的提交如果不在远端分支上，发布页看着一切正常，而默认分支
    上根本没有这版代码——远端 main 会一直停在上一版，直到有人发现。
    """
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if dry_run:
        print("[干跑] 会推送分支 %s" % branch)
        return
    print("=== 推送分支 ===")
    _git("push", "origin", branch)


def _ensure_tag(tag, dry_run):
    """标签不存在就创建并推送；已存在就必须指向当前 HEAD（绝不覆盖）。"""
    exists = _git("rev-parse", "-q", "--verify", "refs/tags/%s" % tag, check=False).returncode == 0
    if exists:
        at = _git("rev-list", "-n", "1", tag).stdout.strip()
        head = _git("rev-parse", "HEAD").stdout.strip()
        if at != head:
            raise ReleaseError("标签 %s 已存在，且指向 %s 而不是当前 HEAD %s；"
                               "换个新版本号，或者先处理掉那个标签" % (tag, at[:8], head[:8]))
        print("标签 %s 已存在且指向 HEAD，跳过创建" % tag)
        return
    if dry_run:
        print("[干跑] 会创建并推送标签 %s" % tag)
        return
    print("=== 打标签 ===")
    _git("tag", "-a", tag, "-m", "快投 %s" % tag)
    _git("push", "origin", tag)


def _ensure_release(slug, tag, payload, token, dry_run):
    url = "https://api.github.com/repos/%s/releases" % slug
    if dry_run:
        print("[干跑] POST %s" % url)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return None
    print("=== 建发布页 ===")
    try:
        rel = _api("POST", url, token, payload)
    except GitHubError as e:
        if e.code != 422:
            raise
        # 422 通常是这个 tag 的发布页已经有了：复用，别重复建
        rel = _api("GET", "%s/tags/%s" % (url, tag), token)
        print("发布页已存在（id=%s），复用它" % rel.get("id"))
    else:
        print("发布页已建：%s" % rel.get("html_url"))
    return rel


def _upload_asset(slug, rel, name, exe, token, force):
    upload = "https://uploads.github.com/repos/%s/releases/%s/assets?name=%s" % (slug, rel["id"], name)
    print("=== 上传资产 ===")
    for a in rel.get("assets", []):
        if a["name"] == name:
            if not force:
                raise ReleaseError("资产 %s 已存在，要覆盖请加 --force" % name)
            _api("DELETE", a["url"], token)
            print("已删掉同名旧资产")
    got = _api("POST", upload, token, raw_path=exe)
    print("资产已上传：%s  %s 字节" % (got.get("name"), got.get("size")))
    print("下载地址：%s" % got.get("browser_download_url"))


def _update_repo_meta(slug, token):
    print("=== 更新仓库门面 ===")
    got = _api("PATCH", "https://api.github.com/repos/%s" % slug, token,
               {"description": REPO_DESCRIPTION})
    print("描述已更新：%s" % got.get("description"))
    got = _api("PUT", "https://api.github.com/repos/%s/topics" % slug, token, {"names": REPO_TOPICS})
    print("topics 已更新：%s" % ", ".join(got.get("names", [])))


# ---------- 入口 ----------

def main():
    parser = argparse.ArgumentParser(description="一键发版：打包、校验、打标签、建 Release、传资产")
    parser.add_argument("tag", nargs="?", help="版本标签，如 v1.9.0")
    parser.add_argument("--notes", default=None, help="发布说明文件（默认 release_notes/<tag>.md）")
    parser.add_argument("--exe", default=DEFAULT_EXE, help="打包产物路径（默认 %s）" % DEFAULT_EXE)
    parser.add_argument("--name", default=None, help="上传后的资产名（默认 Kuaitou-<tag>.exe）")
    parser.add_argument("--draft", action="store_true", help="建为草稿，先不公开")
    parser.add_argument("--prerelease", action="store_true", help="标记为预发布")
    parser.add_argument("--no-build", action="store_true", help="跳过打包，用已有产物")
    parser.add_argument("--dry-run", action="store_true",
                        help="只做只读检查并打印将要发出的载荷；不打包、不写远端、也不要求工作区干净")
    parser.add_argument("--force", action="store_true", help="同名资产已存在时先删掉再传")
    parser.add_argument("--update-repo-meta", action="store_true", help="顺带更新仓库描述与 topics")
    args = parser.parse_args()

    try:
        return _main(args, parser)
    except ReleaseError as e:
        print("发版中断：%s" % e, file=sys.stderr)
        return 1


def _main(args, parser):
    if not args.tag:
        if args.update_repo_meta:
            _update_repo_meta(_remote_repo(), _token())
            return 0
        parser.error("要么给个 tag 发版（如 v1.9.0），要么用 --update-repo-meta 只改仓库门面")

    tag = args.tag
    ver = _check_version(tag)
    print("版本号检查通过：%s" % ver)

    notes_path = args.notes or os.path.join(NOTES_DIR, "%s.md" % tag)
    if not os.path.exists(notes_path):
        raise ReleaseError("找不到发布说明 %s：先把它写好（入库到 release_notes/），或用 --notes 指路径"
                           % notes_path)
    notes = _read_text(notes_path)
    print("发布说明：%s（%s 字）" % (notes_path, len(notes)))

    name = args.name or "Kuaitou-%s.exe" % tag
    if not name.isascii():
        raise ReleaseError("资产名 %s 含非 ASCII 字符：GitHub 会把中文名清成 default.exe，"
                           "请用 --name 换成纯英文" % name)

    if not args.dry_run:
        _check_clean_tree()
    else:
        print("[干跑] 跳过工作区检查、打包与上传，只看会发出什么")

    exe = args.exe if os.path.isabs(args.exe) else os.path.join(ROOT, args.exe)
    if args.no_build or args.dry_run:
        if not args.dry_run and not os.path.exists(exe):
            raise ReleaseError("找不到产物 %s（去掉 --no-build，或先打包一次）" % exe)
    else:
        _build(exe)

    payload = {"tag_name": tag, "target_commitish": TARGET_BRANCH, "name": "快投 %s" % tag,
               "draft": args.draft, "prerelease": args.prerelease, "body": notes}
    slug = _remote_repo()
    print("仓库：%s   产物：%s   资产名：%s" % (slug, exe, name))

    if args.dry_run:
        token = None
    else:
        token = _token()

    _push_branch(args.dry_run)
    _ensure_tag(tag, args.dry_run)
    rel = _ensure_release(slug, tag, payload, token, args.dry_run)
    if not args.dry_run:
        _upload_asset(slug, rel, name, exe, token, args.force)

    if args.update_repo_meta:
        if args.dry_run:
            print("[干跑] 会更新仓库描述与 topics")
        else:
            _update_repo_meta(slug, token)

    if args.dry_run:
        print("干跑结束：什么都没改。确认无误后去掉 --dry-run 再跑一次。")
    else:
        print("发版完成：https://github.com/%s/releases/tag/%s" % (slug, tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
