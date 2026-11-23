#!/usr/bin/env python3
"""
Deterministic, assertion-checked patcher for unpacked Happ Android APK (apktool output).

Applies:
  1. Disable app update checks & forced-update dialogs (e19 + l16 update fetcher).
  2. Strip Sentry SDK (xu5 DSN/config stub + manifest providers + libsentry*.so).
  3. Strip Firebase (FCM / Installations / DataTransport CCT) & disguised pseudo-NTP
     telemetry/push/report endpoints (ntp2-sync.com, time-ntp.com).
  4. Disable RootBeer root checks (l16.u + libtoolChecker.so).
  5. Side-by-side package rename (applicationId, authorities, custom permission,
     shortcuts targetPackage, FileProvider authority, and distinct app_name).
"""

import argparse
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ET.register_namespace("android", ANDROID_NS)


def find_smali_files(work_dir):
    files = []
    for d in sorted(glob.glob(os.path.join(work_dir, "smali*"))):
        for root, _, names in os.walk(d):
            for n in names:
                if n.endswith(".smali"):
                    files.append(os.path.join(root, n))
    return files


def replace_method(smali_text, header_regex, new_body_lines):
    """Replace the body of a single smali method matching header_regex."""
    pattern = re.compile(
        r"(^" + header_regex + r"\r?\n)(.*?)(^\.end method\b)",
        re.MULTILINE | re.DOTALL,
    )
    matches = list(pattern.finditer(smali_text))
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly 1 match for method regex {header_regex!r}, found {len(matches)}"
        )
    m = matches[0]
    replacement = m.group(1) + "\n".join(new_body_lines) + "\n" + m.group(3)
    return smali_text[: m.start()] + replacement + smali_text[m.end() :]


def find_single_smali_with(smali_files, needle):
    hits = []
    for fp in smali_files:
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        if needle in txt:
            hits.append((fp, txt))
    if len(hits) != 1:
        raise RuntimeError(
            f"Expected exactly 1 smali file containing {needle!r}, found {len(hits)}: {[h[0] for h in hits]}"
        )
    return hits[0]


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def patch_update_checker(smali_files):
    # 1a. Update URL list holder (e19.smali in 4.11.2)
    url_file, url_txt = find_single_smali_with(
        smali_files, "https://cdn.jsdelivr.net/gh/Happ-proxy/happ-android@main/release"
    )
    m_cls = re.search(r"^\.class\s+.*?(L[^;\s]+;)", url_txt, re.MULTILINE)
    assert m_cls, f"Could not parse class name in {url_file}"
    cls_desc = m_cls.group(1)
    m_fld = re.search(r"^\.field\s+public static final\s+(\w+):Ljava/util/List;", url_txt, re.MULTILINE)
    assert m_fld, f"Could not parse static List field in {url_file}"
    fld_name = m_fld.group(1)

    new_url_txt = replace_method(
        url_txt,
        r"\.method static constructor <clinit>\(\)V",
        [
            "    .locals 1",
            "    invoke-static {}, Ljava/util/Collections;->emptyList()Ljava/util/List;",
            "    move-result-object v0",
            f"    sput-object v0, {cls_desc}->{fld_name}:Ljava/util/List;",
            "    return-void",
        ],
    )
    assert "happ-android" not in new_url_txt and "pastebin.com" not in new_url_txt
    write_text(url_file, new_url_txt)
    print(f"[OK] Update URLs cleared in {os.path.relpath(url_file)} ({cls_desc}->{fld_name})")

    # 1b. Update fetcher class (l16.smali in 4.11.2) - also hosts RootBeer u()Z
    upd_file, upd_txt = find_single_smali_with(smali_files, '"happ_android"')
    # Stub h(String, q71) -> null (single release fetch)
    m_h = re.search(
        r"^(\.method public [^\r\n]*?\(Ljava/lang/String;L[^;\s]+;\)Ljava/lang/Object;)",
        upd_txt,
        re.MULTILINE,
    )
    assert m_h, f"Could not find update single-fetch method in {upd_file}"
    upd_txt = replace_method(
        upd_txt,
        re.escape(m_h.group(1)),
        [
            "    .locals 1",
            "    const/4 v0, 0x0",
            "    return-object v0",
        ],
    )
    # Stub i(q71) -> empty ArrayList (multi-URL parallel fetch)
    m_i = re.search(
        r"^(\.method public [^\r\n]*?\(L[^;\s]+;\)Ljava/io/Serializable;)",
        upd_txt,
        re.MULTILINE,
    )
    assert m_i, f"Could not find update multi-fetch method in {upd_file}"
    upd_txt = replace_method(
        upd_txt,
        re.escape(m_i.group(1)),
        [
            "    .locals 1",
            "    new-instance v0, Ljava/util/ArrayList;",
            "    invoke-direct {v0}, Ljava/util/ArrayList;-><init>()V",
            "    return-object v0",
        ],
    )
    # Stub RootBeer check u()Z -> false (in the same R8-merged class)
    if "ROOT management app detected!" in upd_txt:
        m_u = re.search(r"^(\.method public \w+\(\)Z)", upd_txt, re.MULTILINE)
        assert m_u, f"Could not find RootBeer ()Z method in {upd_file}"
        upd_txt = replace_method(
            upd_txt,
            re.escape(m_u.group(1)),
            [
                "    .locals 1",
                "    const/4 v0, 0x0",
                "    return v0",
            ],
        )
        print(f"[OK] RootBeer check stubbed to false in {os.path.relpath(upd_file)} ({m_u.group(1)})")
    else:
        raise RuntimeError(f"Expected RootBeer in {upd_file}")

    write_text(upd_file, upd_txt)
    print(f"[OK] Update fetcher stubbed in {os.path.relpath(upd_file)}")


def patch_sentry(smali_files, work_dir):
    # 2a. Stub Sentry options configurator (xu5.smali in 4.11.2)
    s_file, s_txt = find_single_smali_with(smali_files, "ingest.us.sentry.io")
    s_txt = replace_method(
        s_txt,
        r"\.method public [^\r\n]*?\(Lio/sentry/android/core/SentryAndroidOptions;\)V",
        [
            "    .locals 0",
            "    return-void",
        ],
    )
    assert "ingest.us.sentry.io" not in s_txt
    write_text(s_file, s_txt)
    print(f"[OK] Sentry DSN & init options stripped in {os.path.relpath(s_file)}")

    # 2b. Remove native Sentry & RootBeer libs
    removed = []
    for pat in ("lib/*/libsentry.so", "lib/*/libsentry-android.so", "lib/*/libtoolChecker.so"):
        for fp in sorted(glob.glob(os.path.join(work_dir, pat))):
            os.remove(fp)
            removed.append(os.path.relpath(fp, work_dir))
    assert removed, "Expected native libsentry*.so / libtoolChecker.so to exist"
    print(f"[OK] Removed {len(removed)} native telemetry/rootcheck libs: {removed}")


def patch_pseudo_ntp_and_fcm(smali_files):
    # 3a. Stub FirebaseMessagingService.d(RemoteMessage) -> no-op
    fcm_file, fcm_txt = find_single_smali_with(
        smali_files, ".class public final Lsu/happ/proxyutility/firebase/FirebaseMessagingService;"
    )
    fcm_txt = replace_method(
        fcm_txt,
        r"\.method public final d\(L[^;\s]+;\)V",
        [
            "    .locals 0",
            "    return-void",
        ],
    )
    write_text(fcm_file, fcm_txt)
    print(f"[OK] FirebaseMessagingService.d stubbed in {os.path.relpath(fcm_file)}")

    # 3b. Stub remote push poller (wf2.smali in 4.11.2)
    # Identify by fields: Lmf2;, Lfp;, Lce2; and AtomicBoolean
    wf2_hits = []
    for fp in smali_files:
        if os.path.basename(fp) == "wf2.smali":
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            if "Ljava/util/concurrent/atomic/AtomicBoolean;" in txt and "Lfp;" in txt:
                wf2_hits.append((fp, txt))
    assert len(wf2_hits) == 1, f"Expected 1 wf2.smali, found {len(wf2_hits)}"
    wf2_file, wf2_txt = wf2_hits[0]
    wf2_txt = replace_method(
        wf2_txt,
        r"\.method public final a\(L[^;\s]+;\)Ljava/lang/Object;",
        [
            "    .locals 1",
            "    sget-object v0, Lkz8;->a:Lkz8;",
            "    return-object v0",
        ],
    )
    write_text(wf2_file, wf2_txt)
    print(f"[OK] Remote push poller stubbed in {os.path.relpath(wf2_file)}")

    # 3c. Stub ntp2-sync.com/v1remotepush (ap.smali), ntp2-sync.com/v1ackpush (xo.smali),
    #     and time-ntp.com/v1report (cp.smali)
    for needle, label in [
        ("https://ntp2-sync.com/v1remotepush", "v1remotepush"),
        ("https://ntp2-sync.com/v1ackpush", "v1ackpush"),
        ("https://time-ntp.com/v1report", "v1report"),
    ]:
        fp, txt = find_single_smali_with(smali_files, needle)
        assert "Lpq6;" in txt and "Loq6;" in txt, f"Unexpected Result wrapper in {fp}"
        txt = replace_method(
            txt,
            r"\.method public final q\(Ljava/lang/Object;\)Ljava/lang/Object;",
            [
                "    .locals 3",
                "    invoke-static {p1}, Lg41;->S(Ljava/lang/Object;)V",
                "    new-instance v0, Ljava/io/IOException;",
                f'    const-string v1, "{label} disabled"',
                "    invoke-direct {v0, v1}, Ljava/io/IOException;-><init>(Ljava/lang/String;)V",
                "    new-instance v1, Loq6;",
                "    invoke-direct {v1, v0}, Loq6;-><init>(Ljava/lang/Throwable;)V",
                "    new-instance v2, Lpq6;",
                "    invoke-direct {v2, v1}, Lpq6;-><init>(Ljava/lang/Object;)V",
                "    return-object v2",
            ],
        )
        assert needle not in txt
        write_text(fp, txt)
        print(f"[OK] {label} neutered in {os.path.relpath(fp)}")

    # 3d. Defense-in-depth: neutralize v1getpush URL inside R8-merged yo.smali
    yo_file, yo_txt = find_single_smali_with(smali_files, "https://ntp2-sync.com/v1getpush")
    yo_txt = yo_txt.replace("https://ntp2-sync.com/v1getpush", "http://127.0.0.1:1/disabled")
    assert "ntp2-sync.com/v1getpush" not in yo_txt
    write_text(yo_file, yo_txt)
    print(f"[OK] v1getpush URL neutralized in {os.path.relpath(yo_file)}")


def patch_manifest(work_dir, old_id, new_id):
    manifest_path = os.path.join(work_dir, "AndroidManifest.xml")
    tree = ET.parse(manifest_path)
    root = tree.getroot()

    assert root.attrib.get("package") == old_id, f"Unexpected package: {root.attrib.get('package')}"
    root.attrib["package"] = new_id

    name_attr = f"{{{ANDROID_NS}}}name"
    auth_attr = f"{{{ANDROID_NS}}}authorities"

    # Remove C2DM permission & rename custom permission
    remove_root_tags = []
    for child in list(root):
        cname = child.attrib.get(name_attr, "")
        if child.tag == "uses-permission" and cname == "com.google.android.c2dm.permission.RECEIVE":
            remove_root_tags.append(child)
        elif child.tag in ("permission", "uses-permission") and cname.startswith(old_id + "."):
            child.attrib[name_attr] = new_id + cname[len(old_id):]
    for elem in remove_root_tags:
        root.remove(elem)

    # Components to remove completely from <application>
    blocked_components = {
        "io.sentry.android.core.SentryInitProvider",
        "io.sentry.android.core.SentryPerformanceProvider",
        "io.sentry.ndk.SentryNdkPreloadProvider",
        "com.google.firebase.provider.FirebaseInitProvider",
        "com.google.firebase.components.ComponentDiscoveryService",
        "com.google.firebase.iid.FirebaseInstanceIdReceiver",
        "com.google.firebase.messaging.FirebaseMessagingService",
        "su.happ.proxyutility.firebase.FirebaseMessagingService",
        "com.google.android.datatransport.runtime.backends.TransportBackendDiscovery",
        "com.google.android.datatransport.runtime.scheduling.jobscheduling.JobInfoSchedulerService",
        "com.google.android.datatransport.runtime.scheduling.jobscheduling.AlarmManagerSchedulerBroadcastReceiver",
    }
    blocked_metadata = {
        "com.google.firebase.messaging.default_notification_icon",
    }

    app = root.find("application")
    assert app is not None, "Missing <application> in AndroidManifest.xml"

    removed_components = []
    renamed_authorities = []
    for child in list(app):
        cname = child.attrib.get(name_attr, "")
        if cname in blocked_components or (child.tag == "meta-data" and cname in blocked_metadata):
            app.remove(child)
            removed_components.append(cname)
            continue
        if child.tag == "provider":
            auth = child.attrib.get(auth_attr, "")
            if auth.startswith(old_id + "."):
                new_auth = new_id + auth[len(old_id):]
                child.attrib[auth_attr] = new_auth
                renamed_authorities.append((auth, new_auth))

    assert len(removed_components) >= 10, f"Expected >=10 removed components, got {removed_components}"
    assert len(renamed_authorities) >= 2, f"Expected >=2 renamed authorities, got {renamed_authorities}"

    tree.write(manifest_path, encoding="utf-8", xml_declaration=True)
    print(f"[OK] Manifest patched: package={new_id}, removed {len(removed_components)} tracker components, renamed {len(renamed_authorities)} authorities")


def patch_resources(work_dir, old_id, new_id, app_label):
    # 5a. Update app_name / app_tile_name & blank out Firebase keys in strings.xml
    firebase_keys = {
        "gcm_defaultSenderId",
        "google_api_key",
        "google_app_id",
        "google_crash_reporting_api_key",
        "google_storage_bucket",
        "project_id",
    }
    updated_labels = 0
    cleared_fb = 0
    for fp in sorted(glob.glob(os.path.join(work_dir, "res", "values*", "strings.xml"))):
        with open(fp, "r", encoding="utf-8") as f:
            txt = f.read()
        orig = txt
        for key in ("app_name", "app_tile_name"):
            txt, n = re.subn(
                rf'(<string\s+name="{key}"[^>]*>)[^<]*(</string>)',
                rf"\g<1>{app_label}\g<2>",
                txt,
            )
            updated_labels += n
        for key in firebase_keys:
            txt, n = re.subn(
                rf'(<string\s+name="{key}"[^>]*>)[^<]*(</string>)',
                r"\g<1>\g<2>",
                txt,
            )
            cleared_fb += n
        if txt != orig:
            write_text(fp, txt)
    assert updated_labels >= 1, "Failed to update app_name in strings.xml"
    print(f"[OK] Resources patched: app_label='{app_label}' ({updated_labels} entries), cleared {cleared_fb} Firebase string keys")

    # 5b. Update targetPackage in res/xml/*.xml (shortcuts, etc.)
    xml_hits = 0
    for fp in sorted(glob.glob(os.path.join(work_dir, "res", "xml*", "*.xml"))):
        with open(fp, "r", encoding="utf-8") as f:
            txt = f.read()
        if f'android:targetPackage="{old_id}"' in txt:
            txt = txt.replace(f'android:targetPackage="{old_id}"', f'android:targetPackage="{new_id}"')
            write_text(fp, txt)
            xml_hits += 1
    print(f"[OK] Updated targetPackage in {xml_hits} res/xml/*.xml files")

    # 5c. Check smali for hardcoded FileProvider authority string
    fp_hits = 0
    for fp in find_smali_files(work_dir):
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        if f'"{old_id}.provider"' in txt:
            txt = txt.replace(f'"{old_id}.provider"', f'"{new_id}.provider"')
            write_text(fp, txt)
            fp_hits += 1
    print(f"[OK] Updated hardcoded FileProvider authority in {fp_hits} smali files")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work_dir")
    ap.add_argument("--old-id", default="su.happ.proxyutility")
    ap.add_argument("--new-id", default="su.happ.proxymodapp1")
    ap.add_argument("--app-label", default="Happ Clean")
    args = ap.parse_args()

    smali_files = find_smali_files(args.work_dir)
    print(f"Scanning {len(smali_files)} smali files in {args.work_dir}...")

    patch_update_checker(smali_files)
    patch_sentry(smali_files, args.work_dir)
    patch_pseudo_ntp_and_fcm(smali_files)
    patch_manifest(args.work_dir, args.old_id, args.new_id)
    patch_resources(args.work_dir, args.old_id, args.new_id, args.app_label)
    print("ALL PATCHES APPLIED SUCCESSFULLY.")


if __name__ == "__main__":
    main()
