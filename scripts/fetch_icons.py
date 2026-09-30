"""图标素材库维护脚本（开发用，不参与打包、不参与运行）。

图标素材库（icons/）不入库（图标版权归各 App 所有者，见 THIRD-PARTY-NOTICES.md），
所以换台电脑重新打包前，用这个脚本把素材库补回来：

    .build\\venv\\Scripts\\python.exe scripts\\fetch_icons.py          # 只补缺失的
    .build\\venv\\Scripts\\python.exe scripts\\fetch_icons.py --slim   # 顺带统一尺寸瘦身
    .build\\venv\\Scripts\\python.exe scripts\\fetch_icons.py --limit 20

目标包名来自两处：脚本内置的常用应用清单，以及本机 apps_cache.json 里真机装过的应用。
图标来源复用程序在线抓取时用的三个源（应用宝 → 小米商店 → iTunes）。
"""

import argparse
import concurrent.futures
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402

from kuaitou import apps  # noqa: E402
from kuaitou.storage import ICON_DIR, ICON_EXTS  # noqa: E402

LIB_SIZE = 128          # 素材库统一尺寸：界面里图标最大显示 48px，128 够 2 倍屏用
WORKERS = 8

# 常用应用清单（名称用于 iTunes 兜底；包名走应用宝 / 小米商店精确取图）。
# 只影响"素材库预置多少图标"，漏了也不影响功能：程序运行时会按需在线补图。
CURATED = [
    ("微信", "com.tencent.mm"), ("QQ", "com.tencent.mobileqq"),
    ("TIM", "com.tencent.tim"), ("QQ邮箱", "com.tencent.androidqqmail"),
    ("支付宝", "com.eg.android.AlipayGphone"), ("淘宝", "com.taobao.taobao"),
    ("天猫", "com.tmall.wireless"), ("京东", "com.jingdong.app.mall"),
    ("拼多多", "com.xunmeng.pinduoduo"), ("唯品会", "com.achievo.vipshop"),
    ("闲鱼", "com.taobao.idlefish"), ("得物", "com.shizhuang.duapp"),
    ("小红书", "com.xingin.xhs"), ("微博", "com.sina.weibo"),
    ("知乎", "com.zhihu.android"), ("豆瓣", "com.douban.frodo"),
    ("贴吧", "com.baidu.tieba"), ("百度", "com.baidu.searchbox"),
    ("夸克", "com.quark.browser"), ("UC浏览器", "com.UCMobile"),
    ("QQ浏览器", "com.tencent.mtt"), ("Chrome", "com.android.chrome"),
    ("Firefox", "org.mozilla.firefox"), ("Edge", "com.microsoft.emmx"),
    ("美团", "com.sankuai.meituan"), ("饿了么", "me.ele"),
    ("大众点评", "com.dianping.v1"), ("口碑", "com.taobao.movie.android"),
    ("滴滴出行", "com.sdu.didi.psnger"), ("高德地图", "com.autonavi.minimap"),
    ("百度地图", "com.baidu.BaiduMap"), ("腾讯地图", "com.tencent.map"),
    ("携程旅行", "ctrip.android.view"), ("去哪儿旅行", "com.Qunar"),
    ("飞猪", "com.taobao.trip"), ("铁路12306", "com.MobileTicket"),
    ("航旅纵横", "com.umetrip.android.msky.app"), ("同程旅行", "com.tongcheng.android"),
    ("顺丰速运", "com.sf.activity"), ("菜鸟", "com.cainiao.wireless"),
    ("京东到家", "com.jingdong.pdj"), ("盒马", "com.wudaokou.hippo"),
    ("永辉生活", "com.yonghui.yhsh"),
    ("抖音", "com.ss.android.ugc.aweme"), ("抖音极速版", "com.ss.android.ugc.aweme.lite"),
    ("快手", "com.smile.gifmaker"), ("快手极速版", "com.kuaishou.nebula"),
    ("腾讯视频", "com.tencent.qqlive"), ("爱奇艺", "com.qiyi.video"),
    ("优酷视频", "com.youku.phone"), ("芒果TV", "com.hunantv.imgo.activity"),
    ("哔哩哔哩", "tv.danmaku.bili"), ("西瓜视频", "com.ss.android.article.video"),
    ("网易云音乐", "com.netease.cloudmusic"), ("QQ音乐", "com.tencent.qqmusic"),
    ("酷狗音乐", "com.kugou.android"), ("酷我音乐", "cn.kuwo.player"),
    ("喜马拉雅", "com.ximalaya.ting.android"), ("蜻蜓FM", "fm.qingting.qtradio"),
    ("全民K歌", "com.tencent.karaoke"), ("唱吧", "com.changba"),
    ("猫耳FM", "com.missevan"),
    ("微信读书", "com.tencent.weread"), ("掌阅", "com.zhangyue.read"),
    ("番茄免费小说", "com.dragon.read"), ("七猫免费小说", "com.kmxs.reader"),
    ("起点读书", "com.qidian.QDReader"), ("晋江小说阅读", "com.jingdong.app.reader"),
    ("喜马拉雅极速版", "com.ximalaya.ting.lite"),
    ("今日头条", "com.ss.android.article.news"), ("腾讯新闻", "com.tencent.news"),
    ("网易新闻", "com.netease.newsreader.activity"), ("新浪新闻", "com.sina.news"),
    ("虎扑", "com.hupu.games"), ("懂车帝", "com.ss.android.auto"),
    ("汽车之家", "com.cubic.autohome"), ("易车", "com.yiche.autoeasy"),
    ("贝壳找房", "com.lianjia.beike"), ("链家", "com.lianjia.sh.android"),
    ("安居客", "com.anjuke.android.app"), ("58同城", "com.wuba"),
    ("BOSS直聘", "com.hpbr.bosszhipin"), ("智联招聘", "com.zhaopin.social"),
    ("前程无忧", "com.job.android"), ("猎聘", "com.lietou.mishu"),
    ("脉脉", "com.taou.maimai"), ("实习僧", "com.shixiseng.activity"),
    ("钉钉", "com.alibaba.android.rimet"), ("企业微信", "com.tencent.wework"),
    ("飞书", "com.ss.android.lark"), ("腾讯会议", "com.tencent.wemeet.app"),
    ("Zoom", "us.zoom.videomeetings"), ("Teams", "com.microsoft.teams"),
    ("WPS Office", "cn.wps.moffice_eng"), ("金山文档", "cn.wps.moffice_eng.wpslite"),
    ("腾讯文档", "com.tencent.docs"), ("石墨文档", "chuxin.shimo.shimowendang"),
    ("有道云笔记", "com.youdao.note"), ("印象笔记", "com.yinxiang.activity"),
    ("百度网盘", "com.baidu.netdisk"), ("阿里云盘", "com.alicloud.databox"),
    ("迅雷", "com.xunlei.downloadprovider"), ("夸克网盘", "com.quark.clouddrive"),
    ("Notion", "notion.id"), ("Microsoft OneDrive", "com.microsoft.skydrive"),
    ("Google Drive", "com.google.android.apps.docs"),
    ("剪映", "com.lemon.lv"), ("必剪", "com.bilibili.studio"),
    ("快影", "com.kwai.videoeditor"), ("美图秀秀", "com.mt.mtxx.mtxx"),
    ("醒图", "com.xt.retouch"), ("轻颜相机", "com.gorgeous.lite"),
    ("Snapseed", "com.niksoftware.snapseed"), ("Lightroom", "com.adobe.lrmobile"),
    ("Canva", "com.canva.editor"), ("CapCut", "com.lemon.lvoverseas"),
    ("扫描全能王", "com.intsig.camscanner"), ("白描", "com.uzero.baimiao"),
    ("Adobe Acrobat", "com.adobe.reader"), ("微信输入法", "com.tencent.wetype"),
    ("搜狗输入法", "com.sohu.inputmethod.sogou"), ("百度输入法", "com.baidu.input"),
    ("讯飞输入法", "com.iflytek.inputmethod"), ("Gboard", "com.google.android.inputmethod.latin"),
    ("学习强国", "cn.xuexi.android"), ("学习通", "com.chaoxing.mobile"),
    ("作业帮", "com.baidu.homework"), ("小猿搜题", "com.fenbi.android.solar"),
    ("网易有道词典", "com.youdao.dict"), ("百词斩", "com.jiongji.andriod.card"),
    ("墨墨背单词", "com.maimemo.android.momo"), ("扇贝单词", "com.shanbay.words"),
    ("多邻国", "com.duolingo"), ("中国大学MOOC", "com.netease.edu.ucmooc"),
    ("学信网", "cn.com.chsi.chsiapp"), ("Keep", "com.gotokeep.keep"),
    ("咕咚", "com.codoon.gps"), ("悦跑圈", "com.paobu.run"),
    ("薄荷健康", "com.boohee.one"), ("华为运动健康", "com.huawei.health"),
    ("小米运动健康", "com.mi.health"), ("Zepp Life", "com.xiaomi.hm.health"),
    ("同花顺", "com.hexin.plat.android"), ("东方财富", "com.eastmoney.android.berlin"),
    ("雪球", "com.xueqiu.android"), ("涨乐财富通", "com.lphtsccft"),
    ("天天基金", "com.eastmoney.android.fund"), ("富途牛牛", "com.futu.openliu"),
    ("云闪付", "com.unionpay"), ("数字人民币", "cn.gov.pbc.dcep"),
    ("招商银行", "cmb.pb"), ("掌上生活", "com.cmbchina.ccd.pluto.cmbActivity"),
    ("中国工商银行", "com.icbc"), ("中国建设银行", "com.chinamworld.main"),
    ("中国银行", "com.chinamworld.bocmbci"), ("中国农业银行", "com.android.bankabc"),
    ("交通银行", "com.bankcomm.Bankcomm"), ("邮储银行", "com.yitong.mbank.psbc"),
    ("中信银行", "com.ecitic.bank.mobile"), ("平安银行", "com.pingan.paces.ccms"),
    ("浦发银行", "cn.com.spdb.mobilebank.per"), ("兴业银行", "com.cib.xyk"),
    ("光大银行", "com.cebbank.mobile.cemb"), ("民生银行", "cn.com.cmbc.newmbank"),
    ("广发银行", "com.cgbchina.xpt"), ("平安金管家", "com.pingan.papd"),
    ("中国移动", "com.greenpoint.android.mc10086.activity"),
    ("中国联通", "com.sinovatech.unicom.ui"), ("中国电信", "com.ct.client"),
    ("网上国网", "com.sgcc.evs.echarge"),
    ("交管12123", "com.tmri.app.main"), ("个人所得税", "cn.gov.tax.its"),
    ("国家医保服务平台", "cn.hsa.app"), ("国家政务服务平台", "cn.gov.zgzw.app"),
    ("浙里办", "com.hanweb.android.zhejiang.activity"),
    ("鄂汇办", "com.hanweb.android.hubei.activity"),
    ("米家", "com.xiaomi.smarthome"), ("智慧生活", "com.huawei.smarthome"),
    ("海尔智家", "com.haier.uhome.uplus"), ("格力+", "com.gree.iot.gplus"),
    ("小天才", "com.xtc.watch"), ("萤石云视频", "com.videogo"),
    ("和家亲", "com.chinamobile.cmccsmartfamily"),
    ("和平精英", "com.tencent.tmgp.pubgmhd"), ("王者荣耀", "com.tencent.tmgp.sgame"),
    ("英雄联盟手游", "com.tencent.lolm"), ("金铲铲之战", "com.tencent.jkchess"),
    ("原神", "com.miHoYo.Yuanshen"), ("崩坏：星穹铁道", "com.miHoYo.hkrpg"),
    ("绝区零", "com.miHoYo.Nap"), ("第五人格", "com.netease.dwrg"),
    ("蛋仔派对", "com.netease.party"), ("光遇", "com.netease.sky"),
    ("开心消消乐", "com.happyelements.AndroidAnimal"), ("球球大作战", "com.ztgame.bob"),
    ("迷你世界", "com.minitech.miniworld"), ("我的世界", "com.mojang.minecraftpe"),
    ("Steam", "com.valvesoftware.android.steam.community"),
    ("TapTap", "com.taptap"), ("酷安", "com.coolapk.market"),
    ("4399游戏盒", "com.m4399.gamecenter"),
    ("虎牙直播", "com.duowan.kiwi"), ("斗鱼直播", "air.tv.douyu.android"),
    ("YY", "com.duowan.mobile"), ("Soul", "cn.soulapp.android"),
    ("陌陌", "com.immomo.momo"), ("探探", "com.p1.mobile.putong"),
    ("TT语音", "com.duowan.tt"), ("比心", "com.bixin.bixinlive"),
    ("豆包", "com.larus.nova"), ("通义", "com.aliyun.tongyi"),
    ("文心一言", "com.baidu.newapp"), ("Kimi", "com.moonshot.kimichat"),
    ("智谱清言", "com.zhipuai.qingyan"), ("DeepSeek", "com.deepseek.chat"),
    ("腾讯元宝", "com.tencent.hunyuan.app.chat"), ("即梦AI", "com.bytedance.dreamina"),
    ("ChatGPT", "com.openai.chatgpt"), ("Claude", "com.anthropic.claude"),
    ("Gemini", "com.google.android.apps.bard"), ("Perplexity", "ai.perplexity.app.android"),
    ("Discord", "com.discord"), ("Telegram", "org.telegram.messenger"),
    ("WhatsApp", "com.whatsapp"), ("Instagram", "com.instagram.android"),
    ("Facebook", "com.facebook.katana"), ("X", "com.twitter.android"),
    ("YouTube", "com.google.android.youtube"), ("Netflix", "com.netflix.mediaclient"),
    ("Spotify", "com.spotify.music"), ("TikTok", "com.zhiliaoapp.musically"),
    ("Reddit", "com.reddit.frontpage"), ("Pinterest", "com.pinterest"),
    ("LinkedIn", "com.linkedin.android"), ("Slack", "com.Slack"),
    ("Figma", "com.figma.mirror"), ("GitHub", "com.github.android"),
    ("Microsoft Outlook", "com.microsoft.office.outlook"),
    ("Microsoft Word", "com.microsoft.office.word"),
    ("Microsoft Excel", "com.microsoft.office.excel"),
    ("Google 地图", "com.google.android.apps.maps"),
    ("Google 相册", "com.google.android.apps.photos"),
    ("Gmail", "com.google.android.gm"), ("Google Play 商店", "com.android.vending"),
    ("Uber", "com.ubercab"), ("Airbnb", "com.airbnb.android"),
    ("Amazon", "com.amazon.mShop.android.shopping"),
    ("eBay", "com.ebay.mobile"), ("AliExpress", "com.alibaba.aliexpresshd"),
    ("Shopee", "com.shopee.app"), ("Lazada", "com.lazada.android"),
    ("PayPal", "com.paypal.android.p2pmobile"), ("Binance", "com.binance.dev"),
    ("Coinbase", "com.coinbase.android"), ("OKX", "com.okinc.okex.gp"),
    ("MetaMask", "io.metamask"), ("Trust Wallet", "com.wallet.crypto.trustapp"),
    ("LocalSend", "org.localsend.localsend_app"), ("Tailscale", "com.tailscale.ipn"),
    ("ZeroTier One", "com.zerotier.one"), ("Proton VPN", "ch.protonvpn.android"),
    ("Shizuku", "moe.shizuku.privileged.api"), ("MT管理器", "bin.mt.plus"),
    ("ZArchiver Pro", "ru.zdevs.zarchiver.pro"), ("Moonlight", "com.limelight"),
    ("Windows App", "com.microsoft.rdc.androidx"), ("Xmind", "net.xmind.doughnut"),
    ("一个木函", "com.One.WoodenLetter"), ("Via", "mark.via"),
    ("NFC标签助手", "com.fm.nfctools"), ("应用商店", "com.bbk.appstore"),
    ("手机管家", "com.iqoo.secure"), ("主题", "com.bbk.theme"),
    ("相册", "com.vivo.gallery"), ("相机", "com.android.camera"),
    ("文件管理", "com.android.filemanager"), ("日历", "com.bbk.calendar"),
    ("天气", "com.vivo.weather"), ("计算器", "com.android.bbkcalculator"),
    ("录音机", "com.android.bbksoundrecorder"), ("音乐", "com.android.bbkmusic"),
    ("视频", "com.android.VideoPlayer"), ("闹钟时钟", "com.android.BBKClock"),
    ("浏览器", "com.vivo.browser"), ("电子邮件", "com.vivo.email"),
    ("互传", "com.vivo.easyshare"), ("钱包", "com.vivo.wallet"),
    ("游戏中心", "com.vivo.game"), ("设置", "com.android.settings"),
    ("信息", "com.android.mms"), ("电话与联系人", "com.android.contacts"),
    ("V2EX", "com.v2ex.v2ex"), ("什么值得买", "com.smzdm.client.android"),
    ("慢慢买", "com.manmanbuy.bijia"), ("轻启动", "com.wpengapp.lightstart"),
]


def lib_path(pkg):
    return os.path.join(ICON_DIR, pkg + ".webp")


def in_library(pkg):
    """素材库里是否已有该包名（按文件名比对，忽略扩展名与大小写）。"""
    target = pkg.lower()
    try:
        for name in os.listdir(ICON_DIR):
            stem, ext = os.path.splitext(name)
            if ext.lower() in ICON_EXTS and stem.lower() == target:
                return True
    except OSError:
        pass
    return False


def targets():
    """待补清单：内置常用应用 + 本机 apps_cache.json 里真机装过的应用。"""
    items = {pkg: name for name, pkg in CURATED}
    cache_file = os.path.join(ROOT, "apps_cache.json")
    try:
        with open(cache_file, encoding="utf-8") as f:
            devices = (json.load(f) or {}).get("devices") or {}
        for dev in devices.values():
            for app in (dev or {}).get("apps") or []:
                items.setdefault(app.get("package"), app.get("name"))
    except Exception:
        pass
    return [(pkg, name) for pkg, name in items.items() if pkg and not in_library(pkg)]


def fetch_one(pkg, name):
    """按 应用宝 → 小米商店 → iTunes 取一张图，存为素材库里的 128px WebP。返回结果说明。"""
    apps._online_cooldown.clear()          # 批量任务里不让单个源的失败冷却拖慢整批
    for url in (apps._yyb_icon_url(pkg), apps._xiaomi_icon_url(pkg),
                apps._itunes_icon_url(name)):
        if not url:
            continue
        try:
            raw = apps._http_get(url, timeout=15, referer="https://sj.qq.com/")
            if not raw or len(raw) < 1500:
                continue
            webp = apps._icon_bytes_to_webp(raw, size=LIB_SIZE)
            if not webp:
                continue
            with open(lib_path(pkg), "wb") as f:
                f.write(webp)
            return "ok"
        except Exception:
            continue
    return "miss"


def slim_library():
    """把素材库里尺寸不一的图标统一成 128px WebP，并清掉非图标文件。返回 (处理数, 清理数)。"""
    done = cleaned = 0
    for name in sorted(os.listdir(ICON_DIR)):
        path = os.path.join(ICON_DIR, name)
        if not os.path.isfile(path):
            continue
        stem, ext = os.path.splitext(name)
        if ext.lower() not in ICON_EXTS:
            os.remove(path)                        # 打包中间产物 / 历史遗留文件
            cleaned += 1
            print("  清理非图标文件: %s" % name)
            continue
        try:
            with Image.open(path) as im:
                im.load()
                # 已经够小的 WebP 不动它：避免反复有损重编码
                if ext.lower() == ".webp" and max(im.size) <= LIB_SIZE:
                    continue
                im = im.convert("RGBA") if im.mode in ("P", "LA") else im.convert("RGB")
                w, h = im.size
                if w != h:                          # 非方形：居中裁成方形，避免拉伸变形
                    s = min(w, h)
                    im = im.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
                if im.size != (LIB_SIZE, LIB_SIZE):
                    im = im.resize((LIB_SIZE, LIB_SIZE), Image.LANCZOS)
                im.convert("RGB").save(path if ext.lower() == ".webp" else lib_path(stem),
                                       "WEBP", quality=82, method=4)
            if ext.lower() != ".webp":
                os.remove(path)                    # 转换完成后删掉原始的 png / jpg
            done += 1
        except Exception as e:
            print("  跳过 %s: %r" % (name, e))
    return done, cleaned


def dir_size_mb():
    total = 0
    for name in os.listdir(ICON_DIR):
        p = os.path.join(ICON_DIR, name)
        if os.path.isfile(p):
            total += os.path.getsize(p)
    return total / 1024 / 1024


def main():
    ap = argparse.ArgumentParser(description="补齐 / 瘦身图标素材库")
    ap.add_argument("--slim", action="store_true", help="重新编码素材库，统一为 128px WebP")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理多少个目标（调试用）")
    args = ap.parse_args()

    os.makedirs(ICON_DIR, exist_ok=True)
    print("素材库: %s  (%d 个文件, %.1f MB)" % (ICON_DIR, len(os.listdir(ICON_DIR)), dir_size_mb()))

    todo = targets()
    if args.limit:
        todo = todo[:args.limit]
    print("待补图标: %d 个" % len(todo))
    ok = miss = 0
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {ex.submit(fetch_one, pkg, name): (pkg, name) for pkg, name in todo}
            for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                pkg, name = futures[fut]
                res = fut.result()
                ok += res == "ok"
                miss += res == "miss"
                if res == "miss":
                    print("  未找到: %s (%s)" % (pkg, name))
                if i % 20 == 0:
                    print("  ... %d/%d" % (i, len(todo)))
    print("下载完成: 成功 %d, 未找到 %d" % (ok, miss))

    if args.slim:
        print("瘦身中（统一 %dpx WebP）..." % LIB_SIZE)
        before = dir_size_mb()
        done, cleaned = slim_library()
        print("瘦身完成: 处理 %d 个, 清理 %d 个, %.1f MB → %.1f MB"
              % (done, cleaned, before, dir_size_mb()))


if __name__ == "__main__":
    main()
