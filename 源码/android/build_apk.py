#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合收益回测中枢 v1.0.3 · Android APK 增量重打包（模式 B）。

复用同源 WebView 壳（base.apk）：只改 AndroidManifest.xml 的
package / label / versionName / versionCode，替换 assets/index.html，丢旧签名
后换用本产品专属新密钥重签（CN=PnlHub）。classes.dex / resources.arsc /
图标逐字节不变。
"""
import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent   # 仓库根（源码/android → 源码 → 仓库根）
sys.path.insert(0, str(HERE))
from axml import Axml  # noqa: E402

LABEL = "组合收益回测中枢"
VERSION_NAME = "1.0.3"
VERSION_CODE = 4

BASE = pathlib.Path(os.environ.get("BASE", str(HERE / "base.apk")))
# 页面只有一份（网页版即页面源码本体），内嵌页与它逐字节相同，不再单独存副本
# 文件名不带版本号，版本号只写在 README.md 与 版本更新说明.md
PAGE = pathlib.Path(os.environ.get("PAGE", str(ROOT / ("%s_网页版.html" % LABEL))))
DIST = pathlib.Path(os.environ.get("DIST", str(ROOT / "发布版")))
OUT = DIST / ("%s-发布版%s.apk" % (LABEL, VERSION_NAME))


def _find_ks():
    """签名密钥：优先环境变量 PNLHUB_KS，否则取 密钥/ 下第一个 *.jks。"""
    env = os.environ.get("PNLHUB_KS", "").strip()
    if env:
        return pathlib.Path(env)
    cands = sorted((ROOT / "密钥").glob("*.jks")) if (ROOT / "密钥").is_dir() else []
    return cands[0] if cands else ROOT / "密钥" / "keystore.jks"


KS = _find_ks()

_TOOLS_ENV = os.environ.get("ANDROID_TOOLS", "")
TOOLS = pathlib.Path(_TOOLS_ENV) if _TOOLS_ENV else None
if TOOLS is None:
    for _cand in (pathlib.Path.home() / ".workbuddy/binaries/android-tools",
                  pathlib.Path.home() / "android-tools",
                  pathlib.Path("/opt/android-tools")):
        if (_cand / "android-14").is_dir():
            TOOLS = _cand
            break
if TOOLS is None:
    raise SystemExit("未找到 Android 工具链：请设环境变量 ANDROID_TOOLS，"
                     "指向含 android-14/ 与 jdk-*/ 的目录")
BT = TOOLS / "android-14"
_JDKS = sorted(p for p in TOOLS.glob("jdk*") if (p / "bin").is_dir())
JDK = _JDKS[-1] if _JDKS else TOOLS / "jdk-17.0.21+9"
APKSIGNER, ZIPALIGN, AAPT2 = BT / "apksigner", BT / "zipalign", BT / "aapt2"

KS_ALIAS = os.environ.get("PNLHUB_KS_ALIAS", "pnlhub")
KS_PASS = os.environ.get("PNLHUB_KS_PASS", "").strip()
if not KS_PASS:
    _pf = ROOT / "密钥" / "keystore.pass"
    if _pf.is_file():
        KS_PASS = _pf.read_text(encoding="utf-8").strip()
if not KS_PASS:
    raise SystemExit("缺少签名口令：请设环境变量 PNLHUB_KS_PASS，"
                     "或写入 密钥/keystore.pass（.gitignore 已排除该目录）")
PACKAGE = "com.pnlboard.pnlhub"
EXPECT_DN = "CN=PnlHub, OU=PnlBoard, O=PnlBoard, C=CN"
DROP = (".SF", ".RSA", ".DSA", ".MF")


def run(cmd):
    env = dict(os.environ)
    env["PATH"] = str(JDK / "bin") + os.pathsep + env.get("PATH", "")
    env["LC_ALL"] = "C.UTF-8"
    env["JAVA_TOOL_OPTIONS"] = (
        "-Dfile.encoding=UTF-8 -Dsun.jnu.encoding=UTF-8 "
        "-Xmx256m -XX:MaxMetaspaceSize=128m -XX:ReservedCodeCacheSize=32m")
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print((r.stdout or "")[-2000:])
        print((r.stderr or "")[-2000:])
        raise SystemExit("命令失败：%s" % cmd[0])
    return r.stdout


def sha(b):
    return hashlib.sha256(b).hexdigest()


def cert(apk):
    out = run([APKSIGNER, "verify", "--print-certs", apk])
    dn = next((l.split(": ", 1)[1] for l in out.splitlines()
               if "Signer #1 certificate DN:" in l), "")
    fp = next((l.split(": ", 1)[1] for l in out.splitlines()
               if "Signer #1 certificate SHA-256 digest:" in l), "")
    return dn, fp


def main():
    if not KS.exists():
        raise SystemExit("缺少签名密钥：%s\n请按 打包说明.md 第三节自建一套，"
                         "或用 PNLHUB_KS 指定已有 .jks" % KS)
    for p in (BASE, PAGE):
        assert p.exists(), "缺输入：%s" % p
    DIST.mkdir(parents=True, exist_ok=True)

    zin = zipfile.ZipFile(BASE)
    a = Axml(zin.read("AndroidManifest.xml"))
    assert not a.utf8, "字符串池不是 UTF-16"
    old_pkg = a.read_attr(a.attr("manifest", "package"))["raw"]
    old_label = a.read_attr(a.attr("application", "label"))["raw"]
    a.patch([("manifest", "package", PACKAGE),
             ("manifest", "versionCode", VERSION_CODE),
             ("manifest", "versionName", VERSION_NAME),
             ("application", "label", LABEL)])
    manifest = bytes(a.raw)
    print("[1/5] 清单：package %s → %s ｜ label %s → %s" % (old_pkg, PACKAGE, old_label, LABEL))

    page = PAGE.read_bytes()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pnlhub_apk_"))
    unaligned, aligned = tmp / "unaligned.apk", tmp / "aligned.apk"
    with zipfile.ZipFile(unaligned, "w") as zout:
        for info in zin.infolist():
            name = info.filename
            if info.is_dir():
                continue
            if name.startswith("META-INF/") and name.upper().endswith(DROP):
                continue
            if name == "AndroidManifest.xml":
                data = manifest
            elif name == "assets/index.html":
                data = page
                info.compress_type = zipfile.ZIP_DEFLATED
            else:
                data = zin.read(name)
            zi = zipfile.ZipInfo(name, date_time=(1981, 1, 1, 1, 1, 0))
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zi.create_system = 0
            zout.writestr(zi, data)

    run([ZIPALIGN, "-f", "-p", "4", unaligned, aligned])
    run([APKSIGNER, "sign", "--ks", KS, "--ks-key-alias", KS_ALIAS,
         "--ks-pass", "pass:" + KS_PASS, "--key-pass", "pass:" + KS_PASS,
         "--v1-signing-enabled", "true", "--v2-signing-enabled", "true",
         "--v4-signing-enabled", "false", "--out", OUT, aligned])
    print("[2/5] zipalign + 新密钥重签完成（CN=PnlHub）")

    badging = run([AAPT2, "dump", "badging", OUT])
    for key, val in [("package: name='%s'" % PACKAGE, "包名"),
                     ("application-label:'%s'" % LABEL, "应用名"),
                     ("versionName='%s'" % VERSION_NAME, "versionName"),
                     ("versionCode='%d'" % VERSION_CODE, "versionCode"),
                     ("launchable-activity: name='com.pnlboard.app.MainActivity'", "启动 Activity")]:
        assert key in badging, "%s 没改到位" % val
    dn_new, fp_new = cert(OUT)
    assert dn_new == EXPECT_DN, "签名证书不是本产品新密钥：%s" % dn_new
    run([ZIPALIGN, "-c", "4", OUT])
    run(["unzip", "-t", OUT])
    print("[3/5] aapt2 / apksigner / zipalign / unzip 通过 ｜ 新签名 %s" % dn_new)

    with zipfile.ZipFile(OUT) as znew:
        assert znew.read("assets/index.html") == page, "内嵌页面与源码不一致"
        assert len(znew.namelist()) == len(zin.namelist()), "ZIP 条目数变化"
        changed = {"AndroidManifest.xml", "assets/index.html"}
        with zipfile.ZipFile(BASE) as zold:
            for name in zold.namelist():
                if name in changed or name.endswith("/"):
                    continue
                if name.startswith("META-INF/") and name.upper().endswith(DROP):
                    continue
                assert sha(zold.read(name)) == sha(znew.read(name)), "条目被改动：%s" % name
            for critical in ("classes.dex", "resources.arsc"):
                assert sha(zold.read(critical)) == sha(znew.read(critical)), critical
    print("[4/5] classes.dex / resources.arsc / 图标逐字节不变")
    print("\n产物：%s  %.2f MB" % (OUT, OUT.stat().st_size / 1048576))


if __name__ == "__main__":
    main()
