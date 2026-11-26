#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AndroidManifest.xml（二进制 AXML）读写小工具。

只做本项目需要的事：读元素/属性、改属性值（字符串或整数），并把改动写回。
实现方式是把字符串池重建（原有字符串原样保留、新串追加在尾部），
因此所有既有索引不变，只需改目标属性里的 rawValue / data 字段。
"""
import struct
import sys

# chunk 类型
RES_STRING_POOL = 0x0001
RES_XML = 0x0003
RES_XML_RESOURCE_MAP = 0x0180
RES_XML_START_ELEMENT = 0x0102

# 属性值类型
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11

NO_ENTRY = 0xFFFFFFFF


class Axml:
    def __init__(self, data):
        self.raw = bytearray(data)
        self.strings = []
        self.pool_start = 0
        self.pool_end = 0
        self.utf8 = False
        self._read_pool()

    # ---------- 字符串池 ----------
    def _read_pool(self):
        d = self.raw
        typ, hsz, size = struct.unpack_from("<HHI", d, 8)
        assert typ == RES_STRING_POOL, "第一个 chunk 不是字符串池：0x%04x" % typ
        count, style_count, flags, sstart, sstyle = struct.unpack_from("<IIIII", d, 16)
        self.utf8 = bool(flags & 0x100)
        self.pool_start, self.pool_end = 8, 8 + size
        self._count = count

        offs_pos = 8 + hsz
        offsets = struct.unpack_from("<%dI" % count, d, offs_pos)
        base = offs_pos + count * 4
        for off in offsets:
            p = base + off
            if self.utf8:
                n = d[p]
                p += 1
                if n & 0x80:
                    n = ((n & 0x7F) << 8) | d[p]
                    p += 1
                n2 = d[p]
                p += 1
                if n2 & 0x80:
                    n2 = ((n2 & 0x7F) << 8) | d[p]
                    p += 1
                self.strings.append(bytes(d[p:p + n2]).decode("utf-8", "replace"))
            else:
                n = struct.unpack_from("<H", d, p)[0]
                p += 2
                self.strings.append(bytes(d[p:p + n * 2]).decode("utf-16-le", "replace"))

    def _build_pool(self, strings):
        """按给定字符串列表重建池（UTF-16 或 UTF-8），返回 (bytes, 索引表)。"""
        data = bytearray()
        offsets = []
        size_base = 28 + len(strings) * 4
        for s in strings:
            offsets.append(len(data))
            if self.utf8:
                b = s.encode("utf-8")
                if len(b) > 0x7F:
                    raise ValueError("字符串过长，暂不支持：%r" % s)
                data.append(len(b))          # UTF-16 长度（此处与 UTF-8 长度相同）
                data.append(len(b))          # UTF-8 长度
                data += b
                data.append(0)               # 结尾 NUL
            else:
                b = s.encode("utf-16-le")
                if len(b) > 0xFFFE:
                    raise ValueError("字符串过长：%r" % s)
                data += struct.pack("<H", len(b) // 2) + b + b"\x00\x00"
            size_base += 0                   # 尺寸由最终长度决定，下面统一算
        # 4 字节对齐
        while len(data) % 4:
            data.append(0)
        total = 28 + len(strings) * 4 + len(data)
        out = bytearray()
        out += struct.pack("<HHI", RES_STRING_POOL, 28, total)
        out += struct.pack("<IIIII", len(strings), 0, 0x100 if self.utf8 else 0,
                           28 + len(strings) * 4, 0)
        out += struct.pack("<%dI" % len(strings), *offsets)
        out += data
        assert len(out) == total, (len(out), total)
        return bytes(out)

    # ---------- 遍历 ----------
    def chunks(self):
        """依次产出 (type, abs_offset, chunk_size)。"""
        d = self.raw
        off = 8
        while off + 8 <= len(d):
            typ, hsz, size = struct.unpack_from("<HHI", d, off)
            if size < 8 or off + size > len(d):
                break
            yield typ, off, size
            off += size

    def elements(self):
        """产出 (name, abs_offset, [(attr_name, attr_abs_offset), ...])。"""
        d = self.raw
        for typ, off, size in self.chunks():
            if typ != RES_XML_START_ELEMENT:
                continue
            ns, name, astart, asize, acount = struct.unpack_from("<IIHHH", d, off + 16)
            attrs = []
            for i in range(acount):
                a = off + 16 + astart + i * asize
                ans, aname = struct.unpack_from("<II", d, a)
                attrs.append((self.strings[aname] if aname < len(self.strings) else "?", a))
            yield (self.strings[name] if name < len(self.strings) else "?", off, attrs)

    def attr(self, elem_name, attr_name):
        for name, off, attrs in self.elements():
            if name != elem_name:
                continue
            for an, aoff in attrs:
                if an == attr_name:
                    return aoff
        return None

    def read_attr(self, off):
        ns, name, raw, vsize, res0, vtype, data = struct.unpack_from("<IIIHBBI", self.raw, off)
        return {
            "name": self.strings[name] if name < len(self.strings) else "?",
            "type": vtype,
            "data": data,
            "raw": self.strings[raw] if (raw != NO_ENTRY and raw < len(self.strings)) else None,
        }

    # ---------- 修改 ----------
    def patch(self, edits):
        """edits: [(elem_name, attr_name, new_value)]，new_value 为 str 或 int。

        字符串值会被追加进字符串池并改写 rawValue；整数值只改 typed value。
        """
        # 先把需要的字符串追加到池尾
        appended = {}
        for elem, aname, val in edits:
            if isinstance(val, str) and val not in self.strings and val not in appended:
                appended[val] = len(self.strings) + len(appended)

        new_strings = self.strings + list(appended.keys())
        new_pool = self._build_pool(new_strings)
        delta = len(new_pool) - (self.pool_end - self.pool_start)

        # body = 字符串池之后的全部内容（属性偏移都在这段里，随池移动而整体平移）
        body = bytearray(self.raw[self.pool_end:])

        for elem, aname, val in edits:
            abs_off = self.attr(elem, aname)
            assert abs_off is not None, "找不到属性 %s/%s" % (elem, aname)
            rel = abs_off - self.pool_end
            if isinstance(val, str):
                idx = new_strings.index(val)
                # ns, name, rawValue, size, res0, dataType, data
                struct.pack_into("<IIIHBBI", body, rel,
                                 struct.unpack_from("<IIIHBBI", body, rel)[0],
                                 struct.unpack_from("<IIIHBBI", body, rel)[1],
                                 idx, 8, 0, TYPE_STRING, idx)
            else:
                fields = list(struct.unpack_from("<IIIHBBI", body, rel))
                fields[5] = TYPE_INT_DEC
                fields[6] = int(val)
                struct.pack_into("<IIIHBBI", body, rel, *fields)

        out = bytearray(self.raw[:8])
        out += new_pool
        out += body
        struct.pack_into("<HHI", out, 0, RES_XML, 8, len(out))
        self.raw = out
        self.strings = new_strings
        self.pool_end = 8 + len(new_pool)
        return delta

    def save(self, path):
        open(path, "wb").write(bytes(self.raw))

    # ---------- 按绝对偏移改（同名元素有多个时用它） ----------
    def patch_offsets(self, edits):
        """edits: [(attr_abs_offset, new_value)]。

        与 patch() 的区别：直接按属性偏移定位，因此可以精确改到
        「第三个 uses-permission」这种同名元素里的某一个。
        """
        appended = {}
        for _off, val in edits:
            if isinstance(val, str) and val not in self.strings and val not in appended:
                appended[val] = len(self.strings) + len(appended)

        new_strings = self.strings + list(appended.keys())
        new_pool = self._build_pool(new_strings)
        body = bytearray(self.raw[self.pool_end:])

        for off, val in edits:
            rel = off - self.pool_end
            fields = list(struct.unpack_from("<IIIHBBI", body, rel))
            if isinstance(val, str):
                idx = new_strings.index(val)
                fields[2] = idx          # rawValue
                fields[3] = 8            # valueSize
                fields[4] = 0            # res0
                fields[5] = TYPE_STRING
                fields[6] = idx          # data
            else:
                fields[5] = TYPE_INT_DEC
                fields[6] = int(val)
            struct.pack_into("<IIIHBBI", body, rel, *fields)

        out = bytearray(self.raw[:8])
        out += new_pool
        out += body
        struct.pack_into("<HHI", out, 0, RES_XML, 8, len(out))
        self.raw = out
        self.strings = new_strings
        self.pool_end = 8 + len(new_pool)
        return new_strings


def dump(path):
    a = Axml(open(path, "rb").read())
    print("字符串数:", len(a.strings), "| 编码:", "UTF-8" if a.utf8 else "UTF-16", "| 大小:", len(a.raw))
    for name, off, attrs in a.elements():
        if name not in ("manifest", "application", "activity", "provider"):
            continue
        print("\n<%s> @%d" % (name, off))
        for an, aoff in attrs:
            v = a.read_attr(aoff)
            print("   %-26s type=0x%02x data=0x%08x raw=%r" %
                  (v["name"], v["type"], v["data"], v["raw"]))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        dump(sys.argv[2])
    else:
        print("用法: python3 axml.py dump AndroidManifest.xml")
