package com.kuaitou.icondump;

import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.lang.reflect.Method;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.ResolveInfo;
import android.content.res.AssetManager;
import android.content.res.Configuration;
import android.content.res.Resources;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.drawable.Drawable;
import android.os.IBinder;
import android.util.DisplayMetrics;

/**
 * 手机侧取图：把每个能启动的应用的图标渲染成 PNG，打包进一个 zip 文件。
 *
 * 跑法是 dex + app_process，不往手机装任何 App、不申请任何权限：
 *
 *   adb push icondump.dex /data/local/tmp/
 *   adb shell CLASSPATH=/data/local/tmp/icondump.dex app_process /system/bin \
 *       com.kuaitou.icondump.IconDump /data/local/tmp/kuaitou_icons.zip
 *
 * 进程以 shell 身份运行（uid 2000），/data/app 下的 base.apk 对它可读，
 * 所以能直接拿到别的应用的图标资源。
 *
 * 几个绕不开的点（都在真机上踩过）：
 *  1. 这里没法用 ActivityThread 起一个正常的应用上下文 —— 部分 ROM（实测 vivo）
 *     会直接把进程杀掉。所以只能走 ServiceManager 拿 package 服务，再用
 *     IPackageManager$Stub.asInterface 包成接口调，全程反射。
 *  2. getInstalledPackages 返回的是 ParceledListSlice 而不是 List，必须再调
 *     getList()。少这一步会抛 ClassCastException，而 app_process 的默认异常处理
 *     会把它变成 SIGKILL，表现成莫名其妙的「Killed」，很容易误判成 ROM 拦截。
 *  3. 图标资源要从 APK 里解，需要自己拼 AssetManager + Resources（都是隐藏 API，反射）。
 *     显示参数必须给真实密度，否则按 0 密度去挑资源会挑错甚至挑不到。
 */
public final class IconDump {

    private static void log(String s) {
        System.out.println("[icondump] " + s);
        System.out.flush();
    }

    /** 拿到 IPackageManager 接口（反射，见类注释第 1 点）。 */
    private static Object packageManager() throws Exception {
        Class<?> sm = Class.forName("android.os.ServiceManager");
        Object binder = sm.getMethod("getService", String.class).invoke(null, "package");
        if (binder == null) {
            throw new IllegalStateException("package 服务拿不到（ServiceManager 返回 null）");
        }
        Class<?> stub = Class.forName("android.content.pm.IPackageManager$Stub");
        return stub.getMethod("asInterface", IBinder.class).invoke(null, binder);
    }

    /** 所有已安装包的 PackageInfo 列表。 */
    private static List<?> installedPackages(Object ipm) throws Exception {
        Method get = null;
        for (Method m : ipm.getClass().getMethods()) {
            Class<?>[] p = m.getParameterTypes();
            if ("getInstalledPackages".equals(m.getName()) && p.length == 2
                    && p[0] == long.class && p[1] == int.class) {
                get = m;
                break;
            }
        }
        if (get == null) {
            throw new IllegalStateException("找不到 getInstalledPackages(long,int)");
        }
        Object slice = get.invoke(ipm, 0L, 0);
        // 返回的是 ParceledListSlice，不是 List（见类注释第 2 点）
        return (List<?>) slice.getClass().getMethod("getList").invoke(slice);
    }

    /** 有启动图标（桌面能点开）的包名集合；查不到就返回空集合，调用方按「全都要」处理。 */
    private static Set<String> launcherPackages(Object ipm) {
        Set<String> pkgs = new HashSet<String>();
        try {
            Method q = null;
            for (Method m : ipm.getClass().getMethods()) {
                Class<?>[] p = m.getParameterTypes();
                if ("queryIntentActivities".equals(m.getName()) && p.length == 4
                        && p[0] == Intent.class && p[1] == String.class
                        && p[2] == long.class && p[3] == int.class) {
                    q = m;
                    break;
                }
            }
            if (q == null) {
                return pkgs;
            }
            Intent it = new Intent(Intent.ACTION_MAIN);
            it.addCategory(Intent.CATEGORY_LAUNCHER);
            Object slice = q.invoke(ipm, it, null, 0L, 0);
            List<?> list = (List<?>) slice.getClass().getMethod("getList").invoke(slice);
            for (Object o : list) {
                ResolveInfo ri = (ResolveInfo) o;
                if (ri.activityInfo != null && ri.activityInfo.packageName != null) {
                    pkgs.add(ri.activityInfo.packageName);
                }
            }
        } catch (Throwable t) {
            pkgs.clear();               // 拿不到就退化成「所有包都导」，宁多勿漏
        }
        return pkgs;
    }

    /** 用应用自己的 APK 拼一套 Resources，用来解它图标资源里的图（见类注释第 3 点）。 */
    private static Resources resourcesFor(ApplicationInfo ai, DisplayMetrics dm) throws Exception {
        AssetManager am = AssetManager.class.newInstance();
        Method addPath = AssetManager.class.getMethod("addAssetPath", String.class);
        addPath.invoke(am, ai.sourceDir);
        if (ai.splitSourceDirs != null) {           // 图标落在分包里的应用
            for (String p : ai.splitSourceDirs) {
                if (p != null) {
                    addPath.invoke(am, p);
                }
            }
        }
        return (Resources) Resources.class
                .getDeclaredConstructor(AssetManager.class, DisplayMetrics.class, Configuration.class)
                .newInstance(am, dm, new Configuration());
    }

    /** 这个图标是不是自适应图标（Android 8+ 的主流形式）。
     *
     * 按类名逐级往上比，不用 instanceof：AdaptiveIconDrawable 在 API 26 才有，
     * 直接引用它在更老的设备上会 NoClassDefFoundError。
     */
    private static boolean isAdaptive(Drawable d) {
        for (Class<?> c = d.getClass(); c != null; c = c.getSuperclass()) {
            if ("android.graphics.drawable.AdaptiveIconDrawable".equals(c.getName())) {
                return true;
            }
        }
        return false;
    }

    /** 把一个 Drawable 画成 PNG 字节写进 zip。 */
    private static void writeIcon(ZipOutputStream zos, String pkg, Drawable d) throws Exception {
        int w = d.getIntrinsicWidth();
        int h = d.getIntrinsicHeight();
        if (w <= 0 || h <= 0) {
            w = h = 192;               // 自适应图标没有固定尺寸，按 192 兜底
        }
        Bitmap bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        Canvas c = new Canvas(bmp);
        if (isAdaptive(d)) {
            // 自适应图标原样画出来是满幅的方块：背景层会铺满整块画布，只有前景（图标本体）
            // 会被 AdaptiveIconDrawable 自己按可见区缩进去。桌面上看到的是带圆角遮罩的样子，
            // 这里补上同样的圆角裁剪，导出后就跟内置素材库的图标一个风格了。
            // 注意别再自己放大前景：缩放已经由 AdaptiveIconDrawable 内部按可见区做过了，
            // 再放一档会让图标顶满整块画布，比素材库里的明显大一圈。
            float r = Math.min(w, h) * 0.25f;      // 圆角半径取边长的 1/4，与界面上的卡片一致
            Path mask = new Path();
            mask.addRoundRect(new RectF(0, 0, w, h), r, r, Path.Direction.CW);
            c.clipPath(mask);
        }
        d.setBounds(0, 0, w, h);
        d.draw(c);
        zos.putNextEntry(new ZipEntry(pkg + ".png"));
        bmp.compress(Bitmap.CompressFormat.PNG, 100, zos);
        zos.closeEntry();
        bmp.recycle();
    }

    public static void main(String[] args) {
        if (args.length < 1) {
            log("用法：IconDump <输出 zip 路径>");
            System.exit(2);
            return;
        }
        File zipFile = new File(args[0]);
        File parent = zipFile.getParentFile();
        if (parent != null) {
            parent.mkdirs();
        }
        ZipOutputStream zos = null;
        try {
            Object ipm = packageManager();
            List<?> list = installedPackages(ipm);
            Set<String> launchable = launcherPackages(ipm);
            log("已安装 " + list.size() + " 个包，其中可启动 " + launchable.size() + " 个");

            DisplayMetrics dm = new DisplayMetrics();
            dm.setTo(Resources.getSystem().getDisplayMetrics());   // 用真实密度挑资源

            zos = new ZipOutputStream(new BufferedOutputStream(new FileOutputStream(zipFile)));
            int written = 0, skipped = 0;
            for (Object o : list) {
                PackageInfo pi = (PackageInfo) o;
                ApplicationInfo ai = pi.applicationInfo;
                if (ai == null || ai.icon == 0 || ai.sourceDir == null) {
                    continue;
                }
                if (!launchable.isEmpty() && !launchable.contains(pi.packageName)) {
                    continue;          // 桌面点不开的包（系统服务、组件之类）不在应用列表里，不用导
                }
                try {
                    Resources res = resourcesFor(ai, dm);
                    writeIcon(zos, pi.packageName, res.getDrawable(ai.icon, null));
                    written++;
                    if (written % 50 == 0) {
                        log("已导出 " + written + " 个");
                    }
                } catch (Throwable t) {
                    skipped++;
                }
            }
            log("完成：导出 " + written + " 个，跳过 " + skipped + " 个");
        } catch (Throwable t) {
            log("失败：" + t);
            System.exit(3);
        } finally {
            if (zos != null) {
                try {
                    zos.close();
                } catch (Exception ignored) {
                    // 关闭失败无所谓，主流程的成败已经在上面决定了
                }
            }
        }
    }
}
