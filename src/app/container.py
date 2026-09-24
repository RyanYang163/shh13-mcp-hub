"""媒体容器解析与**无损**音轨抽取（MP4/MOV/M4A 与 MKV/WebM）。

为什么自己写解析器而不是调 ffmpeg：Deb 包内不得含预编译二进制（指引 16.4 一票否决），
生命周期脚本也不许联网装依赖。所以核心能力必须由标准库实现；ffmpeg 只作为
「检测到才启用」的可选增强（见 ``ffmpeg.py``）。

这里的抽取是**流复制（stream copy）**：把原始编码帧原样搬进新容器，不重新编码
→ 无质量损失、速度快、CPU 开销极低。

------------------------------------------------------------------ 格式速览

``ISO-BMFF``  MP4 / MOV / M4A / M4V / 3GP 的容器格式，嵌套 box 树::

    ftyp
    moov
      mvhd
      trak                 ← 一条轨道 = 一个 trak
        tkhd               track_id
        mdia
          mdhd             timescale / duration / language
          hdlr             handler_type: soun / vide
          minf
            stbl
              stsd            编码信息（含 esds 等解码器私有数据）
              stsc            样本 → 块
              stsz            每样本字节数
              stco / co64     块在**源文件**中的绝对字节偏移
    mdat                  实际的帧数据

``EBML``      MKV / WebM 的容器格式，元素 =（id vint, size vint, payload）::

    EBML header
    Segment
      Info
      Tracks
        TrackEntry          TrackNumber / TrackType / CodecID / Audio
      Cluster
        Timestamp
        SimpleBlock         轨道号 + 相对时间戳 + 帧数据
        BlockGroup
"""

import os
import struct

# ---------------------------------------------------------------- 通用


class ContainerError(Exception):
    """容器解析或抽取失败（文件损坏、结构异常等）。"""


class UnsupportedFormat(ContainerError):
    """文件类型不在离线支持的范围内。"""


class Reader:
    """给 bytes 包一个 read/seek/tell 接口，让 box/元素遍历器可以复用同一份代码。

    文件句柄天然具备这三个方法，所以遍历器对「文件」和「内存」是同一套实现。
    """

    __slots__ = ("_data", "_pos")

    def __init__(self, data):
        self._data = data
        self._pos = 0

    def read(self, count=-1):
        if count is None or count < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + count]
        self._pos += len(chunk)
        return chunk

    def seek(self, pos, whence=0):
        if whence == 0:
            self._pos = pos
        elif whence == 1:
            self._pos += pos
        else:
            self._pos = len(self._data) + pos
        return self._pos

    def tell(self):
        return self._pos

    def size(self):
        return len(self._data)

    def slice(self, start, end):
        return self._data[start:end]


def _read_exact(handle, count):
    data = handle.read(count)
    if len(data) != count:
        raise ContainerError("文件在读取过程中意外结束（期望 %d 字节，实到 %d）"
                             % (count, len(data)))
    return data


def _read_slice(handle, start, end):
    handle.seek(start)
    return _read_exact(handle, end - start)


CODEC_NAMES = {
    "mp4a": "AAC", "alac": "ALAC", "ac-3": "AC-3", "ec-3": "E-AC-3",
    "Opus": "Opus", "fLaC": "FLAC", "twos": "PCM (s16be)", "sowt": "PCM (s16le)",
    "lpcm": "PCM", "avc1": "H.264", "hvc1": "H.265", "hev1": "H.265",
    "av01": "AV1", "vp09": "VP9", "mp4v": "MPEG-4 Video", "dtsc": "DTS",
    "A_AAC": "AAC", "A_OPUS": "Opus", "A_FLAC": "FLAC", "A_VORBIS": "Vorbis",
    "A_AC3": "AC-3", "A_EAC3": "E-AC-3", "A_MPEG/L3": "MP3", "A_MPEG/L2": "MP2",
    "A_PCM/INT/LIT": "PCM", "A_TRUEHD": "TrueHD", "A_DTS": "DTS",
    "V_MPEG4/ISO/AVC": "H.264", "V_MPEGH/ISO/HEVC": "H.265", "V_VP9": "VP9",
    "V_VP8": "VP8", "V_AV1": "AV1", "S_TEXT/UTF8": "SRT 字幕",
}


def human_codec(codec):
    return CODEC_NAMES.get(codec, codec or "未知")


# ================================================================ ISO-BMFF


def iter_boxes(handle, start, end):
    """遍历 ``[start, end)`` 内的 box。

    产出 ``(type, header_start, payload_start, payload_end)``。
    只读 box 头，不把 payload 读进内存 —— ``mdat`` 再大也不占内存。
    自己维护游标而不依赖 ``handle.tell()``，因此调用方可以在迭代中读文件。
    """
    pos = start
    while pos + 8 <= end:
        handle.seek(pos)
        header = handle.read(8)
        if len(header) < 8:
            return
        size = struct.unpack(">I", header[:4])[0]
        box_type = header[4:8]
        header_size = 8
        if size == 1:
            extended = handle.read(8)
            if len(extended) < 8:
                return
            size = struct.unpack(">Q", extended)[0]
            header_size = 16
        elif size == 0:
            size = end - pos
        if size < header_size or pos + size > end:
            return
        yield box_type, pos, pos + header_size, pos + size
        pos += size


def find_box(handle, start, end, wanted):
    for box_type, _header, payload_start, payload_end in iter_boxes(handle, start, end):
        if box_type == wanted:
            return payload_start, payload_end
    return None


def find_all(handle, start, end, wanted):
    return [(payload_start, payload_end)
            for box_type, _h, payload_start, payload_end in iter_boxes(handle, start, end)
            if box_type == wanted]


def make_box(box_type, payload):
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def _unpack_language(value):
    """mdhd 里的语言是 3 个 5-bit 字符，各加 0x60。"""
    if not value:
        return "und"
    chars = [(value >> 10) & 0x1F, (value >> 5) & 0x1F, value & 0x1F]
    return "".join(chr(c + 0x60) for c in chars if c) or "und"


def _parse_trak(handle, start, end):
    """解析一个 ``trak``（handle 可以是文件或内存 Reader）。"""
    track = {
        "track_id": 0, "handler": None, "codec": None, "kind": "unknown",
        "timescale": 1000, "duration": 0, "language": "und",
        "channels": None, "sample_rate": None, "bit_depth": None,
        "stbl": None, "raw": None,
    }

    tkhd = find_box(handle, start, end, b"tkhd")
    if tkhd:
        version = _read_slice(handle, tkhd[0], tkhd[0] + 4)[0]
        body = tkhd[0] + 4
        if version == 1:
            payload = _read_slice(handle, body, min(body + 28, tkhd[1]))
            track["track_id"] = struct.unpack(">I", payload[16:20])[0]
        else:
            payload = _read_slice(handle, body, min(body + 16, tkhd[1]))
            track["track_id"] = struct.unpack(">I", payload[8:12])[0]

    mdia = find_box(handle, start, end, b"mdia")
    if not mdia:
        return None
    mdia_start, mdia_end = mdia

    mdhd = find_box(handle, mdia_start, mdia_end, b"mdhd")
    if mdhd:
        version = _read_slice(handle, mdhd[0], mdhd[0] + 4)[0]
        body = mdhd[0] + 4
        if version == 1:
            payload = _read_slice(handle, body, min(body + 28, mdhd[1]))
            track["timescale"] = struct.unpack(">I", payload[16:20])[0]
            track["duration"] = struct.unpack(">Q", payload[20:28])[0]
            lang = struct.unpack(">H", payload[28:30])[0] if len(payload) >= 30 else 0
        else:
            payload = _read_slice(handle, body, min(body + 18, mdhd[1]))
            track["timescale"] = struct.unpack(">I", payload[8:12])[0]
            track["duration"] = struct.unpack(">I", payload[12:16])[0]
            lang = struct.unpack(">H", payload[16:18])[0] if len(payload) >= 18 else 0
        track["language"] = _unpack_language(lang)

    hdlr = find_box(handle, mdia_start, mdia_end, b"hdlr")
    if hdlr:
        handler_type = _read_slice(handle, hdlr[0] + 8, hdlr[0] + 12)
        track["handler"] = handler_type.decode("latin-1", "replace")
        track["kind"] = {"soun": "audio", "vide": "video", "text": "subtitle",
                         "sbtl": "subtitle", "subp": "subtitle", "meta": "metadata",
                         "hint": "hint"}.get(track["handler"], "other")

    minf = find_box(handle, mdia_start, mdia_end, b"minf")
    if not minf:
        return None
    stbl = find_box(handle, minf[0], minf[1], b"stbl")
    if not stbl:
        return None
    track["stbl"] = stbl

    stsd = find_box(handle, stbl[0], stbl[1], b"stsd")
    if stsd:
        # stsd = FullBox(4) + entry_count(4)，随后是第一个 sample entry。
        # AudioSampleEntry 的字段偏移（相对 entry 起点）：
        #   0 size | 4 type | 8 reserved(6) | 14 data_ref(2) | 16 version(2) | 18 revision(2)
        #   20 vendor(4) | 24 channels(2) | 26 samplesize(2) | 28 compression(2)
        #   30 packet_size(2) | 32 samplerate(4, 16.16 定点)
        # 注意 28/30 这两个字段容易被漏掉——漏掉就会把 compression 当成采样率读出 0。
        entry_start = stsd[0] + 8
        entry = _read_slice(handle, entry_start, min(entry_start + 36, stsd[1]))
        if len(entry) >= 8:
            track["codec"] = entry[4:8].decode("latin-1", "replace")
        if track["kind"] == "audio" and len(entry) >= 36:
            track["channels"] = struct.unpack(">H", entry[24:26])[0]
            track["bit_depth"] = struct.unpack(">H", entry[26:28])[0]
            track["sample_rate"] = struct.unpack(">I", entry[32:36])[0] >> 16

    return track


def _parse_stbl_index(handle, stbl):
    """取出重建块索引所需的三张表。

    ``stco`` / ``co64`` 里的偏移是**源文件中的绝对字节偏移**，换容器后必须整体平移
    —— 这正是本函数存在的理由。
    """
    stbl_start, stbl_end = stbl
    index = {"chunk_offsets": [], "chunk_64": False, "stsc": [],
             "sample_sizes": [], "default_size": 0}

    stco = find_box(handle, stbl_start, stbl_end, b"stco")
    co64 = None
    if stco is None:
        co64 = find_box(handle, stbl_start, stbl_end, b"co64")
    if stco is not None:
        count = struct.unpack(">I", _read_slice(handle, stco[0] + 4, stco[0] + 8))[0]
        raw = _read_slice(handle, stco[0] + 8, stco[0] + 8 + 4 * count)
        if len(raw) < 4 * count:
            return None
        index["chunk_offsets"] = list(struct.unpack(">%dI" % count, raw))
    elif co64 is not None:
        count = struct.unpack(">I", _read_slice(handle, co64[0] + 4, co64[0] + 8))[0]
        raw = _read_slice(handle, co64[0] + 8, co64[0] + 8 + 8 * count)
        if len(raw) < 8 * count:
            return None
        index["chunk_offsets"] = list(struct.unpack(">%dQ" % count, raw))
        index["chunk_64"] = True
    else:
        return None

    stsc = find_box(handle, stbl_start, stbl_end, b"stsc")
    if stsc:
        count = struct.unpack(">I", _read_slice(handle, stsc[0] + 4, stsc[0] + 8))[0]
        raw = _read_slice(handle, stsc[0] + 8, stsc[0] + 8 + 12 * count)
        for i in range(min(count, len(raw) // 12)):
            first_chunk, samples_per_chunk, _desc = struct.unpack(">III", raw[i * 12:i * 12 + 12])
            index["stsc"].append((first_chunk, samples_per_chunk))

    stsz = find_box(handle, stbl_start, stbl_end, b"stsz")
    if stsz is None:
        return None
    default_size, count = struct.unpack(">II", _read_slice(handle, stsz[0] + 4, stsz[0] + 12))
    index["default_size"] = default_size
    if default_size == 0 and count:
        raw = _read_slice(handle, stsz[0] + 12, stsz[0] + 12 + 4 * count)
        index["sample_sizes"] = list(struct.unpack(">%dI" % min(count, len(raw) // 4), raw))

    return index


def _samples_per_chunk(index, chunk_number):
    """按 stsc 求第 ``chunk_number`` 个块（1 基）含多少样本。"""
    entries = index["stsc"]
    if not entries:
        return 0
    samples = entries[0][1]
    for first_chunk, count in entries:
        if chunk_number >= first_chunk:
            samples = count
        else:
            break
    return samples


def _chunk_sizes(index):
    """每个块在源文件中的字节长度。"""
    sizes = []
    cursor = 0
    sample_sizes = index["sample_sizes"]
    default_size = index["default_size"]
    for chunk_no in range(1, len(index["chunk_offsets"]) + 1):
        count = _samples_per_chunk(index, chunk_no)
        total = 0
        for _ in range(count):
            if default_size:
                total += default_size
            elif cursor < len(sample_sizes):
                total += sample_sizes[cursor]
                cursor += 1
        sizes.append(total)
    return sizes


def parse_mp4(path):
    """解析 MP4/MOV/M4A。返回 dict（含 tracks / mdat 位置 / ftyp）。"""
    size = os.path.getsize(path)
    result = {"path": path, "size": size, "ftyp": b"", "brand": "",
              "mdat": None, "moov": None, "tracks": []}

    with open(path, "rb") as fh:
        for box_type, header_start, payload_start, payload_end in iter_boxes(fh, 0, size):
            if box_type == b"ftyp":
                # 存**整个 box（含 8 字节头）**：抽取时要把 ftyp 原样写到新文件开头，
                # 只存 payload 会写出一个没有 box 头的裸块，输出文件直接不可解析。
                result["ftyp"] = _read_slice(fh, header_start, payload_end)
                result["brand"] = _read_slice(fh, payload_start,
                                              min(payload_start + 4, payload_end)
                                              ).decode("latin-1", "replace")
            elif box_type == b"moov":
                result["moov"] = _read_slice(fh, payload_start, payload_end)
            elif box_type == b"mdat":
                result["mdat"] = (payload_start, payload_end)

    if result["moov"] is None:
        raise ContainerError("没有找到 moov box —— 文件损坏，或不是 MP4/MOV/M4A")
    if result["mdat"] is None:
        raise ContainerError("没有找到 mdat box（文件里没有任何媒体数据）")

    moov = result["moov"]
    reader = Reader(moov)
    for payload_start, payload_end in find_all(reader, 0, len(moov), b"trak"):
        track = _parse_trak(reader, payload_start, payload_end)
        if not track:
            continue
        stbl = track.pop("stbl", None)
        index = _parse_stbl_index(reader, stbl) if stbl else None
        track["index"] = index
        if index:
            track["chunk_sizes"] = _chunk_sizes(index)
            track["sample_count"] = (
                len(index["sample_sizes"]) if index["sample_sizes"]
                else sum(_samples_per_chunk(index, n + 1)
                         for n in range(len(index["chunk_offsets"])))
            )
        else:
            track["chunk_sizes"] = []
            track["sample_count"] = 0
        # trak 的原始字节：重建时逐字节保留，才能不丢 esds / CodecPrivate 之类
        track["raw"] = make_box(b"trak", moov[payload_start:payload_end])
        result["tracks"].append(track)
    return result


def _replace_box(buffer, start, end, path, replace_types, replacement):
    """在 ``buffer[start:end]`` 内按 ``path`` 下钻，替换掉第一个 ``replace_types`` 中的 box。

    纯字节拼接：只改动目标 box，其余字节原样保留 —— 这样 stsd 里的解码器私有数据
    （AAC 的 esds 等）不会在重建过程中丢失。
    """
    wanted = path[0]
    reader = Reader(buffer)
    for box_type, _header, payload_start, payload_end in iter_boxes(reader, start, end):
        if box_type != wanted:
            continue
        if len(path) > 1:
            return _replace_box(buffer, payload_start, payload_end, path[1:],
                                replace_types, replacement)
        for child_type, child_header, _cs, child_end in iter_boxes(reader, payload_start, payload_end):
            if child_type in replace_types:
                return buffer[:child_header] + replacement + buffer[child_end:]
        raise ContainerError("在 %s 内找不到 %s"
                             % (wanted.decode("latin-1"),
                                " 或 ".join(t.decode("latin-1") for t in replace_types)))
    raise ContainerError("box 结构异常：找不到 %s" % wanted.decode("latin-1"))


def extract_mp4_audio(path, track_id, out_path, progress=None):
    """把指定音轨无损搬进新的 M4A 容器。

    1. 按 stsc/stsz 算出每个块的字节长度；
    2. 只复制该音轨的块，按 stco 的顺序连续写进新的 mdat；
    3. 重建 moov（只保留这一条 trak），把 stco/co64 的偏移平移到新位置。

    注意：重建前后 moov 的**长度不变**（stco 条目数不变，只改数值），
    所以可以先按占位偏移量量出长度、再回填真实偏移。
    """
    info = parse_mp4(path)
    track = next((item for item in info["tracks"] if item["track_id"] == track_id), None)
    if track is None:
        raise ContainerError("找不到轨道 %s" % track_id)
    if track["kind"] != "audio":
        raise ContainerError("轨道 %s 不是音频轨（是 %s）" % (track_id, track["kind"]))
    index = track.get("index")
    if not index:
        raise ContainerError("轨道 %s 缺少索引表（stco/stsc/stsz），无法无损抽取" % track_id)

    offsets = index["chunk_offsets"]
    sizes = track["chunk_sizes"]
    if len(offsets) != len(sizes):
        raise ContainerError("块索引不一致：%d 个偏移 vs %d 个长度" % (len(offsets), len(sizes)))
    total_bytes = sum(sizes)

    ftyp = info["ftyp"] or struct.pack(">I4sI4sI", 16, b"ftyp", 0, b"isom", 0)

    # ---- 取出原 moov 里要保留的部件 ----
    moov = info["moov"]
    reader = Reader(moov)
    mvhd = b""
    udta = b""
    for box_type, header, payload_start, payload_end in iter_boxes(reader, 0, len(moov)):
        if box_type == b"mvhd" and not mvhd:
            mvhd = make_box(b"mvhd", moov[payload_start:payload_end])
        elif box_type == b"udta" and not udta:
            udta = make_box(b"udta", moov[payload_start:payload_end])
    if not mvhd:
        raise ContainerError("moov 里没有 mvhd —— 文件结构不完整")

    def build_moov(base_offset):
        chunk_offsets = []
        cursor = base_offset
        for size in sizes:
            chunk_offsets.append(cursor)
            cursor += size
        use_64 = index["chunk_64"] or (chunk_offsets and chunk_offsets[-1] > 0xFFFFFFFF)
        if use_64:
            payload = struct.pack(">II", 0, len(chunk_offsets))
            payload += b"".join(struct.pack(">Q", value) for value in chunk_offsets)
            stco_box = make_box(b"co64", payload)
        else:
            payload = struct.pack(">II", 0, len(chunk_offsets))
            payload += b"".join(struct.pack(">I", value) for value in chunk_offsets)
            stco_box = make_box(b"stco", payload)
        # 路径从 trak 自己开始——track["raw"] 是**整个 trak box**，
        # 它的顶层只有一个 trak，所以第一段必须是 b"trak"。
        trak = _replace_box(track["raw"], 0, len(track["raw"]),
                            [b"trak", b"mdia", b"minf", b"stbl"],
                            (b"stco", b"co64"), stco_box)
        children = [mvhd, trak] + ([udta] if udta else [])
        return make_box(b"moov", b"".join(children))

    # 迭代到稳定：选 stco 还是 co64 会改变 moov 长度，而 mdat 起点又依赖 moov 长度
    moov_bytes = build_moov(0)
    for _ in range(4):
        data_start = len(ftyp) + len(moov_bytes) + 8
        rebuilt = build_moov(data_start)
        if len(rebuilt) == len(moov_bytes):
            moov_bytes = rebuilt
            break
        moov_bytes = rebuilt

    written = 0
    with open(path, "rb") as src, open(out_path, "wb") as dst:
        dst.write(ftyp)
        dst.write(moov_bytes)
        dst.write(struct.pack(">I", total_bytes + 8) + b"mdat")
        buffer_size = 1 << 20
        for chunk_no, (offset, size) in enumerate(zip(offsets, sizes), 1):
            if size <= 0:
                continue
            src.seek(offset)
            remaining = size
            while remaining > 0:
                block = src.read(min(remaining, buffer_size))
                if not block:
                    raise ContainerError("读取源文件时提前结束（第 %d 块）" % chunk_no)
                dst.write(block)
                remaining -= len(block)
            written += size
            if progress and chunk_no % 32 == 0:
                progress(written, total_bytes)
    if progress:
        progress(total_bytes, total_bytes)

    return {
        "output": out_path,
        "bytes": os.path.getsize(out_path),
        "codec": track["codec"],
        "chunks": len(offsets),
        "stream_copy": True,
    }


# ================================================================ EBML / MKV

EBML_SEGMENT = 0x18538067
EBML_INFO = 0x1549A966
EBML_TIMESTAMP_SCALE = 0x2AD7B1
EBML_TRACKS = 0x1654AE6B
EBML_CLUSTER = 0x1F43B675
EBML_TRACK_ENTRY = 0xAE
EBML_TIMESTAMP = 0xE7
EBML_SIMPLE_BLOCK = 0xA3
EBML_BLOCK_GROUP = 0xA0
EBML_BLOCK = 0xA1
EBML_TRACK_NUMBER = 0xD7
EBML_TRACK_TYPE = 0x83
EBML_CODEC_ID = 0x86
EBML_LANGUAGE = 0x22B59C
EBML_NAME = 0x536E
EBML_AUDIO = 0xE1
EBML_SAMPLING_FREQ = 0xB5
EBML_CHANNELS = 0x9F
EBML_BIT_DEPTH = 0x6264
EBML_SEEK_HEAD = 0x114D9B74
EBML_CUES = 0x1C53BB6B
EBML_VOID = 0xEC

UNKNOWN_SIZE = 0x00FFFFFFFFFFFFFF


def read_vint(handle, keep_marker=False):
    """读一个 EBML 变长整数，返回 ``(值, 字节数, 原始字节)``。

    ``keep_marker=True`` 时保留首字节的长度标记位 —— EBML 的**元素 ID 本身就带标记**，
    所以比对 ID 时必须用它（这也是本模块第一版把 Segment 认成不存在的原因）。
    """
    first = handle.read(1)
    if not first:
        raise ContainerError("EBML 读取越界")
    first_byte = first[0]
    if first_byte == 0:
        raise ContainerError("EBML vint 首字节为 0，长度非法")
    length = 1
    mask = 0x80
    while not (first_byte & mask):
        mask >>= 1
        length += 1
        if length > 8:
            raise ContainerError("EBML vint 长度超过 8 字节")
    raw = first + handle.read(length - 1)
    value = first_byte if keep_marker else (first_byte & (mask - 1))
    for byte in raw[1:]:
        value = (value << 8) | byte
    return value, length, raw


def encode_vint(value, length=None):
    """把整数编成 EBML 变长整数（带长度标记）。127 保留给「未知长度」。"""
    if length is None:
        for candidate in range(1, 9):
            if value < (1 << (7 * candidate)) - 1:
                length = candidate
                break
        else:
            raise ValueError("数值过大，无法编码为 EBML vint")
    out = bytearray(length)
    for index in range(length - 1, -1, -1):
        out[index] = value & 0xFF
        value >>= 8
    out[0] |= 1 << (8 - length)
    return bytes(out)


def _encode_id(element_id):
    if element_id <= 0xFF:
        return bytes([element_id])
    if element_id <= 0xFFFF:
        return struct.pack(">H", element_id)
    if element_id <= 0xFFFFFF:
        return struct.pack(">I", element_id)[1:]
    return struct.pack(">I", element_id)


def encode_element(element_id, payload):
    """编一个 EBML 元素：ID 用原始字节编码，size 用最小长度。"""
    return _encode_id(element_id) + encode_vint(len(payload)) + payload


def iter_elements(handle, start, end):
    """遍历 EBML 元素，产出 ``(id, payload_start, payload_end, unknown_size)``。

    ID 通过 ``read_vint(keep_marker=True)`` 得到，因此与 ``EBML_*`` 常量可直接比较。
    自己维护游标，调用方在迭代中读文件也不会打乱遍历。
    """
    pos = start
    while pos < end:
        handle.seek(pos)
        try:
            _, _, id_raw = read_vint(handle, keep_marker=True)
            size, _, _ = read_vint(handle)
        except ContainerError:
            return
        element_id = 0
        for byte in id_raw:
            element_id = (element_id << 8) | byte
        payload_start = handle.tell()
        if size == 127 or size == UNKNOWN_SIZE:
            yield element_id, payload_start, end, True
            return
        payload_end = payload_start + size
        if payload_end > end:
            payload_end = end
        yield element_id, payload_start, payload_end, False
        pos = payload_end


def _decode_uint(payload):
    value = 0
    for byte in payload:
        value = (value << 8) | byte
    return value


def _decode_float(payload):
    if len(payload) == 4:
        return struct.unpack(">f", payload)[0]
    if len(payload) == 8:
        return struct.unpack(">d", payload)[0]
    return 0.0


def _decode_string(payload):
    return payload.rstrip(b"\x00").decode("utf-8", "replace")


def _parse_tracks(raw):
    """解析 Tracks 元素负载，返回轨道列表（含重建 Tracks 所需的原始 TrackEntry）。"""
    tracks = []
    reader = Reader(raw)
    for element_id, payload_start, payload_end, _ in iter_elements(reader, 0, len(raw)):
        if element_id != EBML_TRACK_ENTRY:
            continue
        entry_raw = raw[payload_start:payload_end]
        track = {"track_id": None, "type": None, "codec": None, "language": "und",
                 "name": "", "channels": None, "sample_rate": None,
                 "bit_depth": None, "raw_entry": entry_raw}
        inner = Reader(entry_raw)
        for child_id, child_start, child_end, _ in iter_elements(inner, 0, len(entry_raw)):
            payload = entry_raw[child_start:child_end]
            if child_id == EBML_TRACK_NUMBER:
                track["track_id"] = _decode_uint(payload)
            elif child_id == EBML_TRACK_TYPE:
                track["type"] = _decode_uint(payload)
            elif child_id == EBML_CODEC_ID:
                track["codec"] = _decode_string(payload)
            elif child_id == EBML_LANGUAGE:
                track["language"] = _decode_string(payload)
            elif child_id == EBML_NAME:
                track["name"] = _decode_string(payload)
            elif child_id == EBML_AUDIO:
                audio = Reader(payload)
                for sub_id, sub_start, sub_end, _ in iter_elements(audio, 0, len(payload)):
                    sub = payload[sub_start:sub_end]
                    if sub_id == EBML_CHANNELS:
                        track["channels"] = _decode_uint(sub)
                    elif sub_id == EBML_SAMPLING_FREQ:
                        track["sample_rate"] = _decode_float(sub)
                    elif sub_id == EBML_BIT_DEPTH:
                        track["bit_depth"] = _decode_uint(sub)
        if track["track_id"] is None:
            continue
        track["kind"] = {1: "video", 2: "audio", 17: "subtitle",
                         18: "buttons"}.get(track["type"], "other")
        if not track["language"]:
            track["language"] = "und"
        tracks.append(track)
    return tracks


def parse_mkv(path):
    """解析 MKV/WebM。返回 dict（含 ebml_header / info 元素 / tracks）。"""
    size = os.path.getsize(path)
    result = {"path": path, "size": size, "ebml_header": b"",
              "info_element": b"", "tracks": []}

    with open(path, "rb") as fh:
        if _read_exact(fh, 4) != b"\x1a\x45\xdf\xa3":
            raise ContainerError("不是 EBML 文件（缺少 EBML 头）")
        fh.seek(0)
        _, _, _ = read_vint(fh, keep_marker=True)
        header_size, _, _ = read_vint(fh)
        header_start = fh.tell()
        result["ebml_header"] = (b"\x1a\x45\xdf\xa3"
                                 + encode_vint(header_size)
                                 + _read_slice(fh, header_start, header_start + header_size))

        found_segment = False
        for element_id, payload_start, payload_end, _ in iter_elements(fh, 0, size):
            if element_id != EBML_SEGMENT:
                continue
            found_segment = True
            for child_id, child_start, child_end, _ in iter_elements(fh, payload_start, payload_end):
                if child_id == EBML_TRACKS:
                    result["tracks"] = _parse_tracks(_read_slice(fh, child_start, child_end))
                elif child_id == EBML_INFO:
                    payload = _read_slice(fh, child_start, child_end)
                    result["info_element"] = encode_element(EBML_INFO, payload)
            break

    if not found_segment:
        raise ContainerError("没有找到 Segment 元素 —— 文件可能损坏")
    if not result["tracks"]:
        raise ContainerError("没有找到 Tracks 元素")
    return result


def _block_track_number(payload):
    """SimpleBlock / Block 的头部第一个 vint 即轨道号。"""
    try:
        value, _, _ = read_vint(Reader(payload))
    except ContainerError:
        return None
    return value


def _filter_cluster(raw, keep_track):
    """保留 Cluster 中属于 ``keep_track`` 的块，重建 Cluster 负载；没有块则返回 None。"""
    reader = Reader(raw)
    out = bytearray()
    kept = 0
    for element_id, payload_start, payload_end, _ in iter_elements(reader, 0, len(raw)):
        payload = raw[payload_start:payload_end]
        if element_id == EBML_TIMESTAMP:
            out.extend(encode_element(EBML_TIMESTAMP, payload))
        elif element_id == EBML_SIMPLE_BLOCK:
            if _block_track_number(payload) == keep_track:
                out.extend(encode_element(EBML_SIMPLE_BLOCK, payload))
                kept += 1
        elif element_id == EBML_BLOCK_GROUP:
            inner = Reader(payload)
            for sub_id, sub_start, sub_end, _ in iter_elements(inner, 0, len(payload)):
                if sub_id == EBML_BLOCK and _block_track_number(payload[sub_start:sub_end]) == keep_track:
                    out.extend(encode_element(EBML_BLOCK_GROUP, payload))
                    kept += 1
                    break
        elif element_id == EBML_VOID:
            continue  # Void 只用于占位，重建后大小已不匹配
        else:
            out.extend(encode_element(element_id, payload))  # 例如 CRC-32
    return bytes(out) if kept else None


def extract_mkv_audio(path, track_number, out_path, progress=None):
    """把指定音轨无损搬进新的 MKA（Matroska 音频）容器。

    做法：保留 EBML 头与 Info，重建只含目标轨道的 Tracks，
    再把每个 Cluster 中属于别的轨道的块丢掉。

    **Segment 用未知长度（unknown size）写出**——这样就能边过滤边写盘，
    不必把整条音轨攒在内存里（大文件下这是必须的）。
    Cues 与 SeekHead 被丢弃：它们记录的是源文件的绝对偏移，搬容器后必须重算；
    丢掉只影响「快速跳转」，不影响播放。
    """
    info = parse_mkv(path)
    target = next((item for item in info["tracks"] if item["track_id"] == track_number), None)
    if target is None:
        raise ContainerError("找不到轨道 %s" % track_number)
    if target["kind"] != "audio":
        raise ContainerError("轨道 %s 不是音频轨（是 %s）" % (track_number, target["kind"]))

    new_tracks = encode_element(EBML_TRACKS, encode_element(EBML_TRACK_ENTRY, target["raw_entry"]))
    info_element = info["info_element"] or encode_element(
        EBML_INFO, encode_element(EBML_TIMESTAMP_SCALE, b"\x0f\x42\x40")
    )

    # Segment 用未知长度：0x01FFFFFFFFFFFFFF
    segment_header = _encode_id(EBML_SEGMENT) + b"\x01\xff\xff\xff\xff\xff\xff\xff"

    clusters_written = 0
    bytes_out = 0
    with open(path, "rb") as src, open(out_path, "wb") as dst:
        dst.write(info["ebml_header"])
        dst.write(segment_header)
        dst.write(info_element)
        dst.write(new_tracks)
        bytes_out = len(info["ebml_header"]) + len(segment_header) + len(info_element) + len(new_tracks)

        file_size = info["size"]
        for element_id, payload_start, payload_end, _ in iter_elements(src, 0, file_size):
            if element_id != EBML_SEGMENT:
                continue
            for child_id, child_start, child_end, _ in iter_elements(src, payload_start, payload_end):
                if child_id != EBML_CLUSTER:
                    continue
                filtered = _filter_cluster(_read_slice(src, child_start, child_end), track_number)
                if filtered is None:
                    continue
                blob = encode_element(EBML_CLUSTER, filtered)
                dst.write(blob)
                bytes_out += len(blob)
                clusters_written += 1
                if progress and clusters_written % 16 == 0:
                    progress(child_end, payload_end)
            break
    if progress:
        progress(1, 1)

    return {
        "output": out_path,
        "bytes": bytes_out,
        "codec": target["codec"],
        "chunks": clusters_written,
        "stream_copy": True,
    }


# ================================================================ 统一入口

ISO_BMFF_EXTS = {".mp4", ".m4a", ".m4v", ".mov", ".3gp", ".3g2", ".f4v"}
EBML_EXTS = {".mkv", ".webm", ".mka", ".mks"}

ISO_BMFF_PREFIXES = (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot")


def family_for(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in ISO_BMFF_EXTS:
        return "iso-bmff"
    if ext in EBML_EXTS:
        return "ebml"
    return None


def sniff(path):
    """按魔数判断容器家族（扩展名不可信时用）。"""
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "ebml"
    if len(head) >= 8 and head[4:8] in ISO_BMFF_PREFIXES:
        return "iso-bmff"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "iso-bmff"
    return None


def probe(path):
    """探测媒体文件，返回统一的轨道信息结构。"""
    family = family_for(path) or sniff(path)
    if family is None:
        raise UnsupportedFormat(
            "不支持的文件类型：%s（离线支持 MP4 / MOV / M4A / MKV / WebM）"
            % (os.path.splitext(path)[1] or "无扩展名")
        )

    if family == "iso-bmff":
        info = parse_mp4(path)
        tracks = []
        duration = 0.0
        for track in info["tracks"]:
            seconds = (track["duration"] / track["timescale"]) if track["timescale"] else 0.0
            duration = max(duration, seconds)
            tracks.append({
                "track_id": track["track_id"],
                "kind": track["kind"],
                "codec": track["codec"],
                "codec_name": human_codec(track["codec"]),
                "language": track["language"],
                "channels": track["channels"],
                "sample_rate": track["sample_rate"],
                "bit_depth": track["bit_depth"],
                "duration": seconds,
                "samples": track["sample_count"],
                "extractable": track["kind"] == "audio" and bool(track.get("index")),
            })
        return {"path": path, "size": info["size"], "family": family,
                "brand": info["brand"], "duration": duration, "tracks": tracks,
                "audio_tracks": [t for t in tracks if t["kind"] == "audio"]}

    info = parse_mkv(path)
    tracks = []
    for track in info["tracks"]:
        tracks.append({
            "track_id": track["track_id"],
            "kind": track["kind"],
            "codec": track["codec"],
            "codec_name": human_codec(track["codec"]),
            "language": track["language"],
            "channels": track["channels"],
            "sample_rate": track["sample_rate"],
            "bit_depth": track["bit_depth"],
            "name": track.get("name") or "",
            "duration": 0.0,
            "extractable": track["kind"] == "audio",
        })
    return {"path": path, "size": info["size"], "family": family,
            "brand": "matroska", "duration": 0.0, "tracks": tracks,
            "audio_tracks": [t for t in tracks if t["kind"] == "audio"]}


def suggested_output(path, track, out_dir):
    """缺省输出文件名。ISO-BMFF → .m4a；EBML → .mka。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    if len(stem) > 120:
        stem = stem[:120]
    family = family_for(path) or sniff(path) or "iso-bmff"
    ext = ".mka" if family == "ebml" else ".m4a"
    suffix = ".t%s" % track["track_id"] if track and track.get("track_id") else ""
    return os.path.join(out_dir, stem + suffix + ext)


def extract(path, track_id, out_path, progress=None):
    """按容器家族分派到对应的无损抽取实现。"""
    family = family_for(path) or sniff(path)
    if family is None:
        probe(path)  # 触发 UnsupportedFormat
        family = "iso-bmff"
    if family == "ebml":
        return extract_mkv_audio(path, track_id, out_path, progress=progress)
    return extract_mp4_audio(path, track_id, out_path, progress=progress)
