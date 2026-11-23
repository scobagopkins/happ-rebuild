#!/usr/bin/env python3
"""Rename the applicationId inside AndroidManifest.xml (binary AXML) in place.

No aapt2, no smali, no resource rebuild: only the strings of the manifest
string pool whose value is the package id (or an authority/permission derived
from it) are rewritten, and only when the replacement has the SAME length —
so pool offsets, chunk sizes and every other byte stay valid.

Usage: axml_rename.py in.apk out.apk [--new-id su.happ.proxymodapp1]
"""
import argparse
import struct
import sys
import zipfile

OLD_ID = "su.happ.proxyutility"
# pool entries that are authorities / permissions (not class names)
SUFFIXES = [
    "",
    ".provider",
    ".firebaseinitprovider",
    ".androidx-startup",
    ".SentryInitProvider",
    ".SentryPerformanceProvider",
    ".SentryNdkPreloadProvider",
    ".DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION",
]

RES_STRING_POOL = 0x0001
UTF8_FLAG = 0x0100


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def read_utf8(b, o):
    n = b[o]
    o += 1
    if n & 0x80:
        n = ((n & 0x7F) << 8) | b[o]
        o += 1
    m = b[o]
    o += 1
    if m & 0x80:
        m = ((m & 0x7F) << 8) | b[o]
        o += 1
    return b[o:o + m], o + m + 1


def read_utf16(b, o):
    n = u16(b, o)
    o += 2
    if n & 0x8000:
        n = ((n & 0x7FFF) << 16) | u16(b, o)
        o += 2
    return b[o:o + 2 * n], o + 2 * n + 2


def pool_strings(buf):
    """Yield (byte_start, byte_len, value) for every string in the first pool."""
    if u16(buf, 0) != 0x0003:
        raise SystemExit("not a binary AXML document")
    off = u16(buf, 2)
    end = len(buf)
    while off + 8 <= end:
        ctype = u16(buf, off)
        chdr = u16(buf, off + 2)
        csize = u32(buf, off + 4)
        if csize == 0:
            break
        if ctype == RES_STRING_POOL:
            count = u32(buf, off + 8)
            flags = u32(buf, off + 16)
            strings_start = u32(buf, off + 20)
            utf8 = bool(flags & UTF8_FLAG)
            base = off + strings_start
            for i in range(count):
                s_off = u32(buf, off + chdr + 4 * i)
                start = base + s_off
                if utf8:
                    raw, nxt = read_utf8(buf, start)
                    value = raw.decode("utf-8", "replace")
                else:
                    raw, nxt = read_utf16(buf, start)
                    value = raw.decode("utf-16-le", "replace")
                yield start, len(raw), value, utf8
            return
        off += csize
    raise SystemExit("no string pool")


def rename(manifest, old_id, new_id):
    if len(old_id) != len(new_id):
        raise SystemExit(f"replacement must be the same length: {len(old_id)} vs {len(new_id)}")
    targets = {old_id + s for s in SUFFIXES}
    buf = bytearray(manifest)
    hits = []
    for start, blen, value, utf8 in list(pool_strings(bytes(buf))):
        if value not in targets:
            continue
        new_value = new_id + value[len(old_id):]
        enc = new_value.encode("utf-8") if utf8 else new_value.encode("utf-16-le")
        if len(enc) != blen:
            raise SystemExit(f"encoded length changed for {value!r}")
        buf[start:start + blen] = enc
        hits.append(value)
    return bytes(buf), hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("apk_in")
    ap.add_argument("apk_out")
    ap.add_argument("--old-id", default=OLD_ID)
    ap.add_argument("--new-id", default="su.happ.proxymodapp1")
    a = ap.parse_args()

    zin = zipfile.ZipFile(a.apk_in)
    manifest, hits = rename(zin.read("AndroidManifest.xml"), a.old_id, a.new_id)
    if not hits:
        raise SystemExit("package id not found in the manifest string pool")
    print(f"renamed {len(hits)} pool entries: " + ", ".join(hits), flush=True)

    with zipfile.ZipFile(a.apk_out, "w") as zout:
        for item in zin.infolist():
            name = item.filename
            if name.startswith("META-INF/") and name.split("/")[-1].upper().endswith((".SF", ".RSA", ".DSA", ".EC", ".MF")):
                continue
            zi = zipfile.ZipInfo(name, date_time=item.date_time)
            zi.compress_type = item.compress_type
            zi.external_attr = item.external_attr
            if item.is_dir():
                zout.writestr(zi, b"")
            elif name == "AndroidManifest.xml":
                zi.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(zi, manifest)
            else:
                zout.writestr(zi, zin.read(name))
    print("wrote", a.apk_out)


if __name__ == "__main__":
    main()
