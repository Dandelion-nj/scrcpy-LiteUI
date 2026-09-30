"""应用列表与图标。

scrcpy --list-apps 扫描与按设备缓存（单飞，避免重复拉起 scrcpy）、
本地图标素材库索引与多级容错匹配、在线图标抓取队列。
依赖：storage、device。
"""


import heapq
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher, get_close_matches

from .device import _serial_args, get_devices, get_startupinfo, run_adb
from .storage import (
    APPS_CACHE_STREAM,
    ICON_DIR,
    ICON_EXTS,
    ICON_INDEX_STREAM,
    ICON_SEARCH_DIRS,
    ICON_STREAM_PREFIX,
    SCAN_LOG_STREAM,
    SCRCPY_PATH,
    _ads_usable,
    _write_text,
    ads_path,
    load_config,
    storage_read,
    storage_write,
)

# ============ 本地图标素材库：建索引 + 多策略匹配 ============
# 素材库（icons/）的文件名就是包名。换手机后包名常与库键有细微差异（大小写、
# 分隔符，或多了/少了末段，如库键 cn.amazon.mShop.android 对设备的
# cn.amazon.mShop.android.shopping），所以用多级容错匹配代替精确命中。
#
# 匹配跑在每一次图标请求上，而库有两千多个键，逐键扫描太亏（末级模糊比对尤其贵）。
# 所以启动时就把「包名 → 图标路径」拆成几张查找表：精确 / 归一化 / 前后缀 /
# 末两段重合都退化成几次字典查找，只有模糊兜底才需要遍历，且带廉价预筛。
_ICON_VARIANT_SEGMENTS = frozenset({    # 包名末尾的“版本变体”段：剥掉后再试一轮
    "pro", "lite", "plus", "premium", "hd", "pad", "free", "paid",
    "global", "international", "intl", "overseas", "oversea", "beta",
    "demo", "app", "apk",
})

def _norm_icon_key(s):
    return re.sub(r'[^a-z0-9]', '', (s or "").lower())

class _IconIndex:
    """图标库的多策略查找表（键统一为小写包名，值均为图标路径）。

    exact    完整包名 → 路径
    norm     去掉非字母数字后的包名 → 路径（只差分隔符 / 大小写的换皮包名）
    ahead    库键以该路径开头 → 那个库键（设备包名比库键少一段，取最短库键）
    behind   库键以该路径结尾 → 那个库键（设备包名前面少了几段）
    tail2    末两段 → [库键...]（末两段相同，再比公共后缀长度定优劣）
    """

    def __init__(self):
        self.exact = {}
        self.norm = {}
        self.ahead = {}
        self.behind = {}
        self.tail2 = {}
        self._norm_keys = None      # get_close_matches 用的键快照，add 后失效重建

    def add(self, key, path):
        """登记一个库键。同一键重复登记时保留先到的（抓取的图标优先于素材库）。"""
        key = (key or "").strip().lower()
        if not key or not path:
            return
        self.exact.setdefault(key, path)
        nk = _norm_icon_key(key)
        if nk:
            self.norm.setdefault(nk, path)
        segs = key.split(".")
        for i in range(1, len(segs)):
            # 各级前缀 / 后缀 → 库键：同一片段留最短的库键（最贴近用户实际看到的包名）
            for table, frag in ((self.ahead, ".".join(segs[:i])),
                                (self.behind, ".".join(segs[i:]))):
                cur = table.get(frag)
                if cur is None or len(key) < len(cur):
                    table[frag] = key
        if len(segs) >= 2:
            self.tail2.setdefault("%s.%s" % (segs[-2], segs[-1]), []).append(key)
        self._norm_keys = None

    def _core(self, key):
        """精确 → 归一化 → 前后缀段 → 末两段重合。只查表，不做全表扫描。"""
        hit = self.exact.get(key)
        if hit:
            return hit
        nk = _norm_icon_key(key)
        if nk:
            hit = self.norm.get(nk)
            if hit:
                return hit
        segs = key.split(".")
        if len(segs) < 2:
            return None
        for i in range(len(segs) - 1, 0, -1):     # 库键是设备包名的前缀：取最长（最具体）
            k = ".".join(segs[:i])
            if k in self.exact:
                return self.exact[k]
        for i in range(1, len(segs)):             # 库键是设备包名的后缀：同样取最长
            k = ".".join(segs[i:])
            if k in self.exact:
                return self.exact[k]
        k = self.ahead.get(key)                   # 库键比设备包名多一段
        if k:
            return self.exact[k]
        k = self.behind.get(key)                  # 库键比设备包名多了前缀段
        if k:
            return self.exact[k]
        best, best_common = None, 0
        for k in self.tail2.get("%s.%s" % (segs[-2], segs[-1]), ()):   # 末两段重合
            ks = k.split(".")
            common = 0
            while (common < min(len(segs), len(ks))
                   and segs[-1 - common] == ks[-1 - common]):
                common += 1
            if common > best_common:
                best, best_common = self.exact[k], common
        return best

    def _fuzzy(self, nk, cutoff=0.9):
        """末级兜底：归一化后高度相似（换皮包名只差个别字母，com.foo.bar 对 com.foo.bars）。"""
        if not nk or len(self.norm) < 2:
            return None
        if self._norm_keys is None:
            self._norm_keys = sorted(self.norm)
        m = get_close_matches(nk, self._norm_keys, n=1, cutoff=cutoff)
        return self.norm.get(m[0]) if m else None

    def find(self, pkg):
        """多级匹配：常规链 → 剥掉变体后缀再来一轮 → 模糊兜底。"""
        key = (pkg or "").strip().lower()
        if not key:
            return None
        hit = self._core(key)
        if hit:
            return hit
        segs = key.split(".")
        while len(segs) > 2 and segs[-1] in _ICON_VARIANT_SEGMENTS:
            segs = segs[:-1]
            hit = self._core(".".join(segs))      # com.foo.bar.pro → com.foo.bar
            if hit:
                return hit
        return self._fuzzy(_norm_icon_key(key))

_icon_index = _IconIndex()       # 启动时由 _icon_index_build() 填满
_icon_index_lock = threading.Lock()
_icon_ads_lock = threading.Lock()   # 串行读改写「已缓存图标包名」索引流

def _icon_ads_keys():
    """读回已缓存到 EXE 数据流的图标包名列表。"""
    raw = storage_read(ICON_INDEX_STREAM)
    if not raw:
        return []
    try:
        keys = (json.loads(raw) or {}).get("packages")
        if isinstance(keys, list):
            return [k for k in keys if isinstance(k, str) and k]
    except Exception:
        pass
    return []

def _icon_ads_register(pkg):
    """新抓到的图标写进数据流后，把包名登记到索引流（幂等，加锁避免并发丢更新）。"""
    with _icon_ads_lock:
        keys = _icon_ads_keys()
        if pkg in keys:
            return
        keys.append(pkg)
        storage_write(ICON_INDEX_STREAM,
                      json.dumps({"packages": keys}, ensure_ascii=False))

def _icon_index_build():
    """把各处图标灌进索引。顺序即优先级：手机传回 / 在线抓的图标 > 可写目录 > 内置素材库。

    每次都用全新的索引对象：导入新图标后要重建索引，而旧表里已登记过的键不会被覆盖
    （add 用 setdefault），拿旧表重建等于新图标顶不掉预置素材。
    """
    idx = _IconIndex()
    # 1) EXE 数据流里的图标（运行期抓到的；流不在目录里，只能靠索引流找回）
    for pkg in _icon_ads_keys():
        p = ads_path(ICON_STREAM_PREFIX + pkg + ".webp")
        try:
            if os.path.getsize(p) > 0:
                idx.add(pkg, p)
        except OSError:
            continue
    # 2) 目录里的图标（内置只读素材库 + ADS 不可用时的落盘兜底）
    for d in ICON_SEARCH_DIRS:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext.lower() not in ICON_EXTS:
                continue
            p = os.path.join(d, name)
            try:
                if os.path.getsize(p) <= 0:
                    continue
            except OSError:
                continue
            idx.add(stem, p)                     # 可写目录先登记，优先于只读素材库
    global _icon_index
    with _icon_index_lock:
        _icon_index = idx
    return idx

def _icon_index_add(pkg, path):
    """新抓到的图标写好后登记进索引，后续请求即可立刻命中。"""
    if pkg and path:
        with _icon_index_lock:
            _icon_index.add(pkg, path)

def _write_icon(pkg, data):
    """图标写入：优先写进 EXE 数据流 icon_<包名>.webp，不支持时退回 icons/ 目录。"""
    if _ads_usable():
        p = ads_path(ICON_STREAM_PREFIX + pkg + ".webp")
        try:
            with open(p, "wb") as f:
                f.write(data)
            _icon_ads_register(pkg)
            return p
        except Exception:
            pass
    try:
        os.makedirs(ICON_DIR, exist_ok=True)
        p = os.path.join(ICON_DIR, pkg + ".webp")
        with open(p, "wb") as f:
            f.write(data)
        return p
    except Exception:
        return ""

def find_cached_icon(pkg):
    """从本地素材库匹配图标：精确 → 归一化 → 点分段互为前后缀 → 尾段重合
    → 剥掉变体后缀 → 相似兜底。

    库键是包名，但换手机后常与设备包名有细微差异（大小写、分隔符，或多/少一段，
    例如库键 cn.amazon.mShop.android 对设备 cn.amazon.mShop.android.shopping），
    所以按点分段做双向容错匹配。全程只查启动时建好的索引，不碰网络也不碰 adb。
    """
    with _icon_index_lock:                   # 与 _icon_index_add 同锁，读时不会被改
        return _icon_index.find(pkg)

# 名称别名表：同名不同包名（厂商换皮 / 应用改名）时，按应用名兜底命中素材库。
# 只收录素材库里确实有、且名称足够独特的常用应用；匹配不上就自然跳过，不会误配。
_ICON_NAME_ALIASES = {
    "微信": "com.tencent.mm",
    "QQ": "com.tencent.mobileqq",
    "学习强国": "cn.xuexi.android",
    "支付宝": "com.eg.android.AlipayGphone",
    "淘宝": "com.taobao.taobao",
    "京东": "com.jingdong.app.mall",
    "拼多多": "com.xunmeng.pinduoduo",
    "美团": "com.sankuai.meituan",
    "饿了么": "me.ele",
    "大众点评": "com.dianping.v1",
    "抖音": "com.ss.android.ugc.aweme",
    "快手": "com.smile.gifmaker",
    "哔哩哔哩": "tv.danmaku.bili",
    "微博": "com.sina.weibo",
    "小红书": "com.xingin.xhs",
    "知乎": "com.zhihu.android",
    "豆瓣": "com.douban.frodo",
    "贴吧": "com.baidu.tieba",
    "百度": "com.baidu.searchbox",
    "高德地图": "com.autonavi.minimap",
    "百度地图": "com.baidu.BaiduMap",
    "腾讯视频": "com.tencent.qqlive",
    "爱奇艺": "com.qiyi.video",
    "优酷视频": "com.youku.phone",
    "芒果TV": "com.hunantv.imgo.activity",
    "网易云音乐": "com.netease.cloudmusic",
    "QQ音乐": "com.tencent.qqmusic",
    "酷狗音乐": "com.kugou.android",
    "喜马拉雅": "com.ximalaya.ting.android",
    "微信读书": "com.tencent.weread",
    "掌阅": "com.zhangyue.read",
    "番茄小说": "com.dragon.read",
    "今日头条": "com.ss.android.article.news",
    "钉钉": "com.alibaba.android.rimet",
    "企业微信": "com.tencent.wework",
    "飞书": "com.ss.android.lark",
    "腾讯会议": "com.tencent.wemeet.app",
    "WPS Office": "cn.wps.moffice_eng",
    "百度网盘": "com.baidu.netdisk",
    "迅雷": "com.xunlei.downloadprovider",
    "剪映": "com.lemon.lv",
    "美图秀秀": "com.mt.mtxx.mtxx",
    "滴滴出行": "com.sdu.didi.psnger",
    "12306": "com.MobileTicket",
    "携程旅行": "ctrip.android.view",
    "去哪儿旅行": "com.Qunar",
    "铁路12306": "com.MobileTicket",
    "唯品会": "com.achievo.vipshop",
    "闲鱼": "com.taobao.idlefish",
    "得物": "com.shizhuang.duapp",
    "懂车帝": "com.ss.android.auto",
    "汽车之家": "com.cubic.autohome",
    "58同城": "com.wuba",
    "BOSS直聘": "com.hpbr.bosszhipin",
    "同花顺": "com.hexin.plat.android",
    "东方财富": "com.eastmoney.android.berlin",
    "雪球": "com.xueqiu.android",
    "云闪付": "com.unionpay",
    "中国移动": "com.greenpoint.android.mc10086.activity",
    "中国联通": "com.sinovatech.unicom.ui",
    "中国电信": "com.ct.client",
    "招商银行": "cmb.pb",
    "中国银行": "com.chinamworld.bocmbci",
    "中国建设银行": "com.chinamworld.main",
    "QQ邮箱": "com.tencent.androidqqmail",
    "网易邮箱大师": "com.netease.mail",
    "Keep": "com.gotokeep.keep",
    "学习通": "com.chaoxing.mobile",
    "作业帮": "com.baidu.homework",
    "网易有道词典": "com.youdao.dict",
    "墨墨背单词": "com.maimemo.android.momo",
    "扫描全能王": "com.intsig.camscanner",
    "夸克": "com.quark.browser",
    "UC浏览器": "com.UCMobile",
    "QQ浏览器": "com.tencent.mtt",
    "火狐浏览器": "org.mozilla.firefox",
    "Chrome": "com.android.chrome",
    "酷安": "com.coolapk.market",
    "TapTap": "com.taptap",
    "4399游戏盒": "com.m4399.gamecenter",
    "萤石云视频": "com.videogo",
    "米家": "com.xiaomi.smarthome",
    "智慧生活": "com.huawei.smarthome",
    "vivo官网": "com.vivo.space",
    "抖音极速版": "com.ss.android.ugc.aweme.lite",
    "快手极速版": "com.kuaishou.nebula",
    "今日头条极速版": "com.ss.android.article.lite",
    "全民K歌": "com.tencent.karaoke",
    "唱吧": "com.changba",
    "虎牙直播": "com.duowan.kiwi",
    "斗鱼直播": "air.tv.douyu.android",
    "和平精英": "com.tencent.tmgp.pubgmhd",
    "王者荣耀": "com.tencent.tmgp.sgame",
    "原神": "com.miHoYo.Yuanshen",
    "第五人格": "com.netease.dwrg",
    "开心消消乐": "com.happyelements.AndroidAnimal",
    "百度贴吧": "com.baidu.tieba",
    "孔夫子旧书网": "com.kongfz.app",
    "豆包": "com.larus.nova",
    "通义": "com.aliyun.tongyi",
    "文心一言": "com.baidu.newapp",
    # 海外应用：Android 上的应用名多是英文原名，按名字兜底即可命中（国内商店没有这类包）
    "Telegram": "org.telegram.messenger",
    "ChatGPT": "com.openai.chatgpt",
    "Claude": "com.anthropic.claude",
    "Discord": "com.discord",
    "TikTok": "com.zhiliaoapp.musically",
    "Slack": "com.Slack",
    "Reddit": "com.reddit.frontpage",
    "Figma": "com.figma.mirror",
    "GitHub": "com.github.android",
    "LinkedIn": "com.linkedin.android",
    "Gmail": "com.google.android.gm",
    "Google Drive": "com.google.android.apps.docs",
    "Google Photos": "com.google.android.apps.photos",
    "Google Maps": "com.google.android.apps.maps",
    "Gboard": "com.google.android.inputmethod.latin",
    "YouTube": "com.google.android.youtube",
    "WhatsApp": "com.whatsapp",
    "Instagram": "com.instagram.android",
    "Facebook": "com.facebook.katana",
    "Spotify": "com.spotify.music",
    "Netflix": "com.netflix.mediaclient",
    "Uber": "com.ubercab",
    "eBay": "com.ebay.mobile",
    "Amazon": "com.amazon.mShop.android.shopping",
    "Shopee": "com.shopee.app",
    "Lazada": "com.lazada.android",
    "AliExpress": "com.alibaba.aliexpresshd",
    "CapCut": "com.lemon.lvoverseas",
    "Binance": "com.binance.dev",
    "OKX": "com.okinc.okex.gp",
    "Coinbase": "com.coinbase.android",
    "MetaMask": "io.metamask",
    "Trust Wallet": "com.wallet.crypto.trustapp",
    "PayPal": "com.paypal.android.p2pmobile",
    "Zoom": "us.zoom.videomeetings",
    "Microsoft Teams": "com.microsoft.teams",
    "Outlook": "com.microsoft.office.outlook",
    "OneDrive": "com.microsoft.skydrive",
    "Notion": "notion.id",
    "Duolingo": "com.duolingo",
    "Steam": "com.valvesoftware.android.steam.community",
    "V2EX": "com.v2ex.v2ex",
    # 国内应用补充：名字独特且素材库大概率已有，兜底命中率比精确包名匹配高
    "番茄免费小说": "com.dragon.read",
    "腾讯地图": "com.tencent.map",
    "夸克浏览器": "com.quark.browser",
    "网易云": "com.netease.cloudmusic",
    "哔哩哔哩动画": "tv.danmaku.bili",
    "滴答清单": "cn.ticktick.task",
    "薄荷健康": "com.boohee.one",
    "扇贝单词": "com.shanbay.words",
    "小猿搜题": "com.fenbi.android.solar",
    "猿辅导": "com.fenbi.android.leo",
    "美团外卖": "com.sankuai.meituan.takeoutnew",
    "饿了么外卖": "me.ele",
    "闲鱼二手": "com.taobao.idlefish",
    "迅雷看看": "com.xunlei.downloadprovider",
    "知乎日报": "com.zhihu.android.app.night",
    "航班管家": "com.flightmanager.view",
    "小米商城": "com.xiaomi.shop",
    "华为商城": "com.vmall.client",
    "有道翻译官": "com.youdao.translator",
    "喜马拉雅极速版": "com.ximalaya.ting.lite",
}

# 应用名末尾的“版本变体”词：别名表里只登记主名，剥掉变体再查一轮
# （"哔哩哔哩HD"、"抖音极速版" 这类名字在全名兜底时会漏，剥后缀后就能对上主名）
_NAME_VARIANT_SUFFIXES = (
    "极速版", "国际版", "海外版", "专业版", "企业版", "免费版", "精简版", "轻量版",
    "青春版", "标准版", "正式版", "测试版", "体验版", "极简版", "高级版", "纯净版",
    "hd", "pro", "lite", "plus", "premium", "free", "global", "tv",
)

def _name_variants(nk):
    """归一化名字的候选序列：先原样，再逐级剥掉末尾的版本变体词。"""
    out = [nk]
    while True:
        for suf in _NAME_VARIANT_SUFFIXES:
            if len(nk) - len(suf) >= 2 and nk.endswith(suf):
                nk = nk[:-len(suf)]
                out.append(nk)
                break
        else:
            return out

def _norm_app_name(s):
    return re.sub(r'[\s\-_·.，。,：:！!？?（）()]+', '', (s or "")).lower()

_ITUNES_COUNTRIES = ("cn", "us")     # iTunes 商店地区：CN 优先，海外应用回落到 US 店
# iTunes 按 User-Agent 做地区访问控制：US 店用浏览器 UA 一律 403，只有 curl 这类
# 非浏览器 UA 才放行（CN 店反过来，两种 UA 都通）。所以海外店单独换 UA 再查一次。
_UA_CURL = "curl/8.4.0"

# 归一化后的名字再查一次：带空格 / 标点 / 大小写差异的名字（"WPS Office" 对 "wps office"）也能命中
_ICON_ALIASES_NORM = {}
for _n, _p in _ICON_NAME_ALIASES.items():
    _ICON_ALIASES_NORM.setdefault(_norm_app_name(_n), _p)
# 只有纯英文名才做模糊匹配：中文名短，差一个字往往就是另一个应用，误配代价太高
_ICON_ALIASES_ASCII = sorted(n for n in _ICON_ALIASES_NORM if n.isascii())

def _alias_icon(name):
    """按应用名找别名包名对应的素材，四级兜底：

    原名直查 → 归一化后查（吃掉空格/标点/大小写）→ 剥掉末尾版本变体再查
    （"哔哩哔哩HD" 对 "哔哩哔哩"）→ 英文名相似度 ≥0.85（只有纯英文名才做，
    中文名短，差一个字往往就是另一个应用，误配代价太高）。

    命中的别名包名还要再走一遍 find_cached_icon：素材库缺这个图标时不能
    死在这里，得让它返回 None 去尝试在线抓取。
    """
    raw = (name or "").strip()
    if not raw:
        return None
    alias = _ICON_NAME_ALIASES.get(raw)
    if alias is None:
        nk = _norm_app_name(raw)
        for cand in _name_variants(nk) if nk else ():
            alias = _ICON_ALIASES_NORM.get(cand)
            if alias is None and cand.isascii() and len(cand) >= 4:
                m = get_close_matches(cand, _ICON_ALIASES_ASCII, n=1, cutoff=0.85)
                alias = _ICON_ALIASES_NORM.get(m[0]) if m else None
            if alias:
                break
    return find_cached_icon(alias) if alias else None

# ============ 在线图标源（应用宝 / 小米商店 / iTunes）============
# 只补充素材库缺失的图标；不再从手机 APK 提取（那条路径要反复跑 adb，
# 会把 adb 信号量占满、拖死整个界面）。
_UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_online_lock = threading.Lock()
_online_cooldown = {}          # 源 -> 冷却截止时间戳（网络/限流故障，短冷却自愈）
_ONLINE_COOLDOWN_SEC = 300
_XIAOMI_PLACEHOLDER = "02f4849db3f7e487599e257b336d57b159d425b04"

# 系统通用名在 App Store 必然错配（如“设置/信息/电话”），iTunes 源直接跳过
_ITUNES_GENERIC_NAMES = {
    "设置", "信息", "电话", "相机", "相册", "浏览器", "音乐", "视频", "日历", "天气",
    "计算器", "录音机", "文件管理", "主题", "钱包", "互传", "邮件", "电子邮件", "联系人",
    "时钟", "闹钟时钟", "指南针", "手机管家", "扫描", "翻译机", "开关控制", "一键锁屏",
    "意见反馈", "游戏中心", "应用商店", "vivo摄影", "vivo健康", "vivo官网", "原子笔记",
    "电话与联系人", "智能遥控", "智慧生活", "系统跟踪", "Android System Angle",
}

def _http_get(url, timeout=12, referer=None, ua=None):
    headers = {"User-Agent": ua or _UA_BROWSER, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def _online_ok(src):
    with _online_lock:
        return time.time() > _online_cooldown.get(src, 0)

def _online_fail(src):
    with _online_lock:
        _online_cooldown[src] = time.time() + _ONLINE_COOLDOWN_SEC

def _yyb_icon_url(pkg):
    """应用宝详情页 SSR 内嵌记录，按包名精确取图标（Android 原生方形，256px）。"""
    if not _online_ok("yyb"):
        return None
    try:
        html = _http_get("https://sj.qq.com/appdetail/" + urllib.parse.quote(pkg),
                         timeout=12).decode("utf-8", "replace")
        m = re.search(
            r'"pkg_name":"' + re.escape(pkg)
            + r'","app_id":"\d+","name":"[^"]*","icon":"([^"]+)"', html)
        if not m:
            return None  # 未上架 / 页面无该包记录（非故障，不冷却）
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("http://"):
            u = "https://" + u[7:]
        if u.endswith(".svg") or "yyb-icon" in u:
            return None
        return u
    except urllib.error.HTTPError as e:
        if e.code not in (404, 400):   # 404=未上架；403/5xx=限流或故障，冷却
            _online_fail("yyb")
        return None
    except Exception:
        _online_fail("yyb")
        return None

def _xiaomi_icon_url(pkg):
    """小米应用商店详情页，取第一个 PNG 缩略图 hash 拼 l360 直链（包名必须出现在页面中防重定向占位）。"""
    if not _online_ok("mi"):
        return None
    try:
        url = "https://app.mi.com/details?id=" + urllib.parse.quote(pkg)
        html = _http_get(url, timeout=10).decode("utf-8", "replace")
        if pkg not in html:
            return None
        m = re.search(r'thumbnail/PNG/l\d+/AppStore/([0-9a-f]{40})', html)
        if not m or m.group(1) == _XIAOMI_PLACEHOLDER:
            return None
        return "https://file.market.xiaomi.com/thumbnail/PNG/l360/AppStore/" + m.group(1)
    except urllib.error.HTTPError as e:
        if e.code not in (404, 400):
            _online_fail("mi")
        return None
    except Exception:
        _online_fail("mi")
        return None

def _itunes_icon_url(name):
    """iTunes Search API 按应用名取图（bb 方形满版，无 iOS 圆角）；名称相似度校验防同名错配。

    先查 CN 店（中文应用名命中率高），没有再查 US 店：海外应用（Google / Telegram 等）
    根本不在 CN 店上架，只查 CN 会一律抓空。命中多条时逐条按名称相似度挑，
    比只看第一条稳（搜索首条常是推广位的另一款应用）。
    """
    if not name or name in _ITUNES_GENERIC_NAMES or not _online_ok("itunes"):
        return None
    nk = _norm_app_name(name)
    if not nk:
        return None
    for country in _ITUNES_COUNTRIES:
        try:
            # limit 必须用 1：实测 limit>1 会触发 403（iTunes 对 /search 有查询限流）。
            # 用 term 精确名称，US 店海外应用名就是英文原名，第一条就是目标。
            u = ("https://itunes.apple.com/search?term=" + urllib.parse.quote(name)
                 + "&country=" + country + "&entity=software&limit=1")
            j = json.loads(_http_get(
                u, timeout=10, ua=None if country == "cn" else _UA_CURL,
            ).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                continue        # 该店没有这条记录，换下一个地区
            _online_fail("itunes")
            return None
        except Exception:
            _online_fail("itunes")
            return None
        for item in (j.get("results") or [])[:1]:
            art = item.get("artworkUrl512") or ""
            b = _norm_app_name(item.get("trackName", ""))
            if not art or not b:
                continue
            if nk in b or b in nk or SequenceMatcher(None, nk, b).ratio() >= 0.72:
                return re.sub(r'/\d+x\d+bb\.(?:jpg|png)$', '/512x512bb.jpg', art) or None
    return None

def _icon_bytes_to_webp(raw, size=256):
    """下载的图标统一为 size×size webp；校验分辨率/单色/透明，不合格返回 None。

    运行期抓图用默认 256（缓存进数据流）；素材库维护脚本传 128 直接出小图。
    """
    from PIL import Image, ImageStat
    im = Image.open(io.BytesIO(raw))
    im.load()
    if min(im.size) < 96:
        return None
    if im.mode in ("P", "LA"):
        im = im.convert("RGBA")
    elif im.mode == "CMYK":
        im = im.convert("RGB")
    w, h = im.size
    if w != h:
        s = min(w, h)
        im = im.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    if im.size != (size, size):
        im = im.resize((size, size), Image.LANCZOS)
    if im.mode == "RGBA":
        alpha = im.getchannel("A")
        if alpha.getextrema()[0] < 250:
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=alpha)
            im = bg
        else:
            im = im.convert("RGB")
    elif im.mode != "RGB":
        im = im.convert("RGB")
    sd = ImageStat.Stat(im.resize((24, 24))).stddev
    if sum(sd) / 3 < 6:
        return None
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=82, method=4)
    return buf.getvalue()

def fetch_online_icon(pkg, name=None):
    """按 应用宝 → 小米 → iTunes 顺序在线获取图标，转 webp 永久缓存。返回缓存路径或 None。"""
    cached = find_cached_icon(pkg) or _alias_icon(name)
    if cached:
        return cached
    for url in (_yyb_icon_url(pkg), _xiaomi_icon_url(pkg), _itunes_icon_url(name)):
        if not url:
            continue
        try:
            raw = _http_get(url, timeout=15, referer="https://sj.qq.com/")
            if not raw or len(raw) < 1500:
                continue
            webp = _icon_bytes_to_webp(raw)
            if not webp:
                continue
            path = _write_icon(pkg, webp)
            if not path:
                continue
            _icon_index_add(pkg, path)
            return path
        except Exception:
            continue
    return None

# 图标获取调度：只剩在线队列（4 worker，秒级，完全不占用 adb）。
# 关键点：任何请求都不再阻塞 —— 命中素材库立即返回；否则入队后立即返回 None，
# 前端按延迟重试，抓到之后下一次请求自然命中。
_online_heap = []
_in_online = set()
_icon_cooldown = {}         # pkg -> 冷却截止；抓不到时短冷却，避免反复入队
_ICON_COOLDOWN_SEC = 600
_icon_cv = threading.Condition()
_icon_seq = 0

def _app_name(pkg):
    with _apps_lock:
        for apps in (_apps_cache or {}).values():
            for a in apps:
                if a.get("package") == pkg:
                    return a.get("name")
    return None

def _online_worker_loop():
    while True:
        with _icon_cv:
            while not _online_heap:
                _icon_cv.wait()
            _seq, pkg, qname = heapq.heappop(_online_heap)
            if pkg not in _in_online:
                continue                    # 重复入队产生的旧条目
        try:
            path = fetch_online_icon(pkg, qname or _app_name(pkg))
        except Exception:
            path = None
        with _icon_cv:
            _in_online.discard(pkg)
            if path:
                _icon_cooldown.pop(pkg, None)
            else:
                _icon_cooldown[pkg] = time.time() + _ICON_COOLDOWN_SEC

for _i in range(4):
    threading.Thread(target=_online_worker_loop,
                     name="icon-online-%d" % _i, daemon=True).start()

def submit_icon(pkg, name=None):
    """把包名投进在线图标队列。已缓存 / 冷却中 / 已在队列里则跳过。"""
    global _icon_seq
    if not pkg or find_cached_icon(pkg):
        return False
    with _icon_cv:
        if time.time() < _icon_cooldown.get(pkg, 0) or pkg in _in_online:
            return False
        _in_online.add(pkg)
        heapq.heappush(_online_heap, (_icon_seq, pkg, name))
        _icon_seq += 1
        _icon_cv.notify()
        return True

def request_icon(pkg, name=None):
    """只读素材库：命中就返回路径；否则后台补抓并立即返回 None。

    绝不等待网络，避免 HTTP 线程被拖住几十秒把界面卡死。
    """
    cached = find_cached_icon(pkg)
    if not cached:
        cached = _alias_icon(name)       # 换皮 / 改名包：按应用名兜底命中素材库
    if cached:
        return cached
    submit_icon(pkg, name=name)
    return None

def prefetch_icons(items):
    """items: pkg 字符串或 {"package":..,"name":..} 字典列表。只入在线队列，秒级返回。"""
    for it in items:
        if isinstance(it, dict):
            submit_icon(it.get("package"), name=it.get("name"))
        else:
            submit_icon(it)

_apps_cache = {}             # {设备序列号: [{"name":..,"package":..}]}（内存缓存，按设备各一份）
_apps_lock = threading.Lock()
# 首次扫描需推送 scrcpy-server 并在设备端起 Java 进程逐个取应用名，无线 adb 下明显偏慢，
# 超时值给足，避免扫到一半被中断成空结果。
APPS_SCAN_TIMEOUT = 120

def _load_apps_cache():
    """启动时读取上次扫描结果；同一台设备可直接出列表，不必每次启动都重扫。

    结构 {"devices": {序列号: {"apps": [...]}}}；旧版单设备格式直接忽略
    （重扫一次即可），不做兼容转换。
    """
    global _apps_cache
    raw = storage_read(APPS_CACHE_STREAM)
    if not raw:
        return
    try:
        devs = (json.loads(raw) or {}).get("devices")
        if isinstance(devs, dict):
            cache = {}
            for serial, item in devs.items():
                apps = (item or {}).get("apps")
                if isinstance(apps, list) and apps:
                    cache[serial] = apps
            _apps_cache = cache
    except Exception:
        pass

def _save_apps_cache(devices):
    """把全部设备的缓存写回存储。devices 为已拍好的快照，避免在锁外遍历活字典。"""
    storage_write(APPS_CACHE_STREAM, json.dumps({"devices": devices}, ensure_ascii=False))
def _log_scan_failure(msg):
    """扫描异常时记一份小日志：窗口程序没有控制台，出问题只能靠日志排查。"""
    _write_text(SCAN_LOG_STREAM, time.strftime("%Y-%m-%d %H:%M:%S ") + msg)

def _parse_apps_output(out):
    """解析 scrcpy --list-apps 的输出（形如 ` - 应用名    com.foo.bar`）。

    按行取「首个字段=应用名、末个字段=包名」，包名做一次格式校验防脏数据；
    重名包只留一条。返回按名称排序的 [{"name":..,"package":..}]。
    """
    apps = []
    seen = set()
    for line in out.split('\n'):
        line = line.strip()
        if line.startswith('-') or line.startswith('*'):
            line = line.lstrip('-*').strip()
            parts = re.split(r'\s{2,}', line)
            if len(parts) >= 2:
                label = parts[0].strip()
                pkg = parts[-1].strip()
                if label and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+', pkg) \
                        and pkg not in seen:
                    seen.add(pkg)
                    apps.append({"name": label, "package": pkg})
    apps.sort(key=lambda x: x["name"].lower())
    return apps

def _scan_apps(serial):
    """真正执行一次扫描。返回 None 表示扫描失败（未连接/超时/报错），
    与"扫描成功但设备上确实没有应用"（返回空列表）区分开，避免用失败结果覆盖缓存。"""
    try:
        r = subprocess.run([SCRCPY_PATH] + _serial_args(serial) + ["--list-apps"], capture_output=True,
                          encoding="utf-8", errors="replace",
                          timeout=APPS_SCAN_TIMEOUT, cwd=os.path.dirname(SCRCPY_PATH),
                          startupinfo=get_startupinfo(), creationflags=0x08000000)
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        _log_scan_failure("scan exception: %r" % (e,))
        return None
    apps = _parse_apps_output(out)
    if not apps:
        _log_scan_failure("rc=%s | no apps parsed | tail:\n%s" % (r.returncode, out[-2000:]))
        if r.returncode != 0:
            return None
    return apps

def list_apps(serial, force=False):
    """获取指定设备的应用列表（按序列号各缓存一份）。并发请求共享同一次扫描
    （单飞），避免重复拉起 scrcpy 把首次连接拖慢。"""
    if not serial:
        devs = get_devices()
        serial = devs[0] if devs else None
    if not serial:
        return []
    if not force:
        with _apps_lock:
            cached = _apps_cache.get(serial)
        if cached is not None:
            return cached
    with _apps_lock:
        # 等锁期间其他请求可能已经扫完，直接复用
        if not force:
            cached = _apps_cache.get(serial)
            if cached is not None:
                return cached
        apps = _scan_apps(serial)
        if apps is None:
            return _apps_cache.get(serial, [])   # 扫描失败则退回已有缓存，绝不用空结果顶替
        _apps_cache[serial] = apps
        snapshot = {s: {"apps": a} for s, a in _apps_cache.items()}
    _save_apps_cache(snapshot)
    # 后台预取：素材库直接命中；缺失的进在线队列，不阻塞按需请求
    prefetch_icons(apps)
    return apps

_icon_index_build()
_load_apps_cache()

# ============ 从手机导入图标 ============
# 手机上的取图工具把图标导出成一个目录（文件名是包名最省事，是应用名也能认），这里把整个
# 目录拉回来，转成 webp 写进可写图标库。导入的图标在查找顺序上排在预置素材库前面：手机传
# 回来的是这台设备上真实在用的那张图，比我们预置的通用图准；没导入时自然还是用预置的。
_ICON_PULL_TIMEOUT = 300        # 拉几百个图标走 USB 也就几秒，无线慢些，耐心给足

def _looks_like_pkg(name):
    return bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+', name or ""))

def _pkg_by_app_name():
    """已扫到的应用「归一化名 → 包名」：图标文件名可能是「微信.png」这种。"""
    table = {}
    with _apps_lock:
        for apps in (_apps_cache or {}).values():
            for a in apps:
                nk = _norm_app_name(a.get("name") or "")
                if nk:
                    table.setdefault(nk, a.get("package"))
    return table

def _ingest_icon_file(path, name_table):
    """一个图标文件算出它的包名；认不出来返回 None。文件名优先当包名，其次当应用名。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    if _looks_like_pkg(stem):
        return stem
    return name_table.get(_norm_app_name(stem))

def import_icons_from_device(serial=None, remote_dir=None):
    """把手机上 remote_dir 里的图标收回来入库，返回 {ok, msg, imported, skipped}。

    只认图片后缀；拉回临时目录、转完 webp 就删掉，磁盘上不留下这些中间文件。
    入库后重建图标索引，导入的图标因此优先于随包预置的素材库。
    """
    cfg = load_config()
    remote_dir = (remote_dir or cfg.get("icon_import_dir") or "").strip()
    if not remote_dir:
        return {"ok": False, "msg": "还没填手机上的图标目录：先到设置里填上"}
    if not serial:
        devs = get_devices()
        serial = devs[0] if devs else None
    if not serial:
        return {"ok": False, "msg": "没有已连接的设备"}
    out, err, code = run_adb(["shell", "ls", "-1", remote_dir], timeout=30, serial=serial)
    text = out + err
    if code != 0 or "No such" in text:
        return {"ok": False, "msg": "手机上找不到 %s：确认取图工具导出到哪个目录" % remote_dir}
    images = [n.strip() for n in out.splitlines()
              if os.path.splitext(n.strip())[1].lower() in ICON_EXTS]
    if not images:
        return {"ok": False, "msg": "%s 里没有图片：先在手机上把图标导出到这个目录" % remote_dir}
    tmp = tempfile.mkdtemp(prefix="kuaitou_icons_")
    imported, skipped = 0, 0
    try:
        _, perr, pcode = run_adb(["pull", remote_dir, tmp],
                                 timeout=_ICON_PULL_TIMEOUT, serial=serial)
        if pcode != 0:
            return {"ok": False, "msg": "拉取图标失败：%s" % ((perr or "").strip() or "adb pull 出错")}
        name_table = _pkg_by_app_name()
        for root, _dirs, files in os.walk(tmp):
            for fn in files:
                if os.path.splitext(fn)[1].lower() not in ICON_EXTS:
                    continue
                full = os.path.join(root, fn)
                pkg = _ingest_icon_file(full, name_table)
                if not pkg:
                    skipped += 1
                    continue
                try:
                    with open(full, "rb") as f:
                        webp = _icon_bytes_to_webp(f.read())
                except Exception:
                    webp = None
                if not webp or not _write_icon(pkg, webp):
                    skipped += 1
                    continue
                imported += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if imported:
        _icon_index_build()          # 重建索引：导入的图标要排在预置素材库前面
        msg = "已导入 %d 个图标，应用列表会优先用它们" % imported
        if skipped:
            msg += "；%d 个跳过（认不出包名或图片不合格）" % skipped
        return {"ok": True, "msg": msg, "imported": imported, "skipped": skipped}
    return {"ok": False, "imported": 0, "skipped": skipped,
            "msg": "没能导入：%d 个文件都认不出包名或图片不合格（文件名建议直接用包名）" % skipped}

def launch_icon_tool(package=None, serial=None):
    """在手机上打开取图工具（monkey 拉起它的启动页），返回 {ok, msg}。"""
    pkg = (package or load_config().get("icon_tool_package") or "").strip()
    if not pkg:
        return {"ok": False, "msg": "还没填取图工具的包名：先到设置里填上再点这个按钮"}
    if not serial:
        devs = get_devices()
        serial = devs[0] if devs else None
    if not serial:
        return {"ok": False, "msg": "没有已连接的设备"}
    out, err, code = run_adb(["shell", "monkey", "-p", pkg, "-c",
                              "android.intent.category.LAUNCHER", "1"],
                             timeout=30, serial=serial)
    text = out + err
    if code != 0 or "No activities found" in text or "aborted" in text.lower():
        return {"ok": False, "msg": "没能打开 %s：确认这台手机上装了这个应用" % pkg}
    return {"ok": True, "msg": "已在手机上打开取图工具：导出图标后，回来点「从手机导入图标」"}
