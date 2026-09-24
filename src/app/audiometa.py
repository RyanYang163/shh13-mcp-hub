"""纯音频文件的元数据读取（WAV / FLAC / MP3 / OGG）。

容器化的文件（M4A / MP4 / MOV / MKV / WebM）走 ``container.py``；
这里只处理没有容器层的裸音频流。

全部用标准库解析文件头，不依赖任何外部程序 —— 未安装 ffmpeg 也能用。
"""

import os
import struct

from .container import ContainerError, UnsupportedFormat


def _read_slice(fh, start, count):
    fh.seek(start)
    return fh.read(count)


def read_wav(fh, size):
    """RIFF/WAVE：fmt 块给格式，data 块大小给时长。"""
    header = _read_slice(fh, 0, 12)
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ContainerError("不是合法的 WAV 文件")
    info = {"format": "WAV", "codec": "PCM", "channels": None, "sample_rate": None,
            "bit_depth": None, "duration": 0.0, "bitrate": 0, "tags": {}}
    data_size = 0
    pos = 12
    while pos + 8 <= size:
        chunk_header = _read_slice(fh, pos, 8)
        if len(chunk_header) < 8:
            break
        chunk_id = chunk_header[:4]
        chunk_size = struct.unpack("<I", chunk_header[4:8])[0]
        if chunk_id == b"fmt ":
            body = _read_slice(fh, pos + 8, min(chunk_size, 40))
            if len(body) >= 16:
                audio_format, channels, rate, byte_rate, _align, bits = struct.unpack(
                    "<HHIIHH", body[:16])
                info["channels"] = channels
                info["sample_rate"] = rate
                info["bit_depth"] = bits
                info["bitrate"] = byte_rate * 8
                info["codec"] = {
                    1: "PCM", 3: "IEEE Float", 6: "A-law", 7: "µ-law",
                    0xFFFE: "PCM (extensible)", 0x55: "MP3", 0x2000: "AC-3",
                }.get(audio_format, "格式 0x%X" % audio_format)
        elif chunk_id == b"data":
            data_size = chunk_size
        elif chunk_id == b"LIST":
            body = _read_slice(fh, pos + 8, min(chunk_size, 4096))
            info["tags"].update(_parse_riff_info(body))
        pos += 8 + chunk_size + (chunk_size % 2)
    if info["sample_rate"] and info["bit_depth"] and info["channels"]:
        bytes_per_second = (info["sample_rate"] * info["channels"] * info["bit_depth"]) / 8
        if bytes_per_second > 0:
            info["duration"] = data_size / bytes_per_second
    return info


def _parse_riff_info(body):
    """解析 LIST/INFO 里的 ``INAM`` / ``IART`` 之类的标签。"""
    tags = {}
    mapping = {b"INAM": "title", b"IART": "artist", b"IPRD": "album",
               b"ICMT": "comment", b"ICRD": "date", b"IGNR": "genre",
               b"IPRT": "track", b"ISFT": "encoder"}
    # 前 4 字节是 "INFO"
    pos = 4 if body[:4] == b"INFO" else 0
    while pos + 8 <= len(body):
        tag_id = body[pos:pos + 4]
        tag_size = struct.unpack("<I", body[pos + 4:pos + 8])[0]
        if tag_size > len(body) - pos - 8:
            break
        raw = body[pos + 8:pos + 8 + tag_size].rstrip(b"\x00")
        name = mapping.get(tag_id)
        if name and raw:
            tags[name] = raw.decode("utf-8", "replace")
        pos += 8 + tag_size + (tag_size % 2)
    return tags


def read_flac(fh, size):
    """FLAC：STREAMINFO 块里直接带采样率、声道、位深与总样本数。"""
    if _read_slice(fh, 0, 4) != b"fLaC":
        raise ContainerError("不是合法的 FLAC 文件")
    info = {"format": "FLAC", "codec": "FLAC", "channels": None, "sample_rate": None,
            "bit_depth": None, "duration": 0.0, "bitrate": 0, "tags": {}}
    pos = 4
    while pos + 4 <= size:
        block_header = _read_slice(fh, pos, 4)
        if len(block_header) < 4:
            break
        is_last = bool(block_header[0] & 0x80)
        block_type = block_header[0] & 0x7F
        block_size = int.from_bytes(block_header[1:4], "big")
        if block_type == 0 and block_size >= 34:
            body = _read_slice(fh, pos + 4, 34)
            packed = int.from_bytes(body[10:18], "big")
            sample_rate = (packed >> 44) & 0xFFFFF
            channels = ((packed >> 41) & 0x07) + 1
            bits = ((packed >> 36) & 0x1F) + 1
            total_samples = packed & 0xFFFFFFFFF
            info["sample_rate"] = sample_rate
            info["channels"] = channels
            info["bit_depth"] = bits
            if sample_rate:
                info["duration"] = total_samples / sample_rate
                info["bitrate"] = int(size * 8 / info["duration"]) if info["duration"] else 0
        elif block_type == 4:
            body = _read_slice(fh, pos + 4, min(block_size, 65536))
            info["tags"].update(_parse_vorbis_comment(body))
        pos += 4 + block_size
        if is_last:
            break
    return info


def _parse_vorbis_comment(body):
    """Vorbis comment 块：小端长度 + ``KEY=value``。"""
    tags = {}
    if len(body) < 8:
        return tags
    try:
        vendor_len = struct.unpack("<I", body[:4])[0]
        cursor = 4 + vendor_len
        if cursor + 4 > len(body):
            return tags
        count = struct.unpack("<I", body[cursor:cursor + 4])[0]
        cursor += 4
        for _ in range(min(count, 256)):
            if cursor + 4 > len(body):
                break
            length = struct.unpack("<I", body[cursor:cursor + 4])[0]
            cursor += 4
            if length > len(body) - cursor:
                break
            entry = body[cursor:cursor + length].decode("utf-8", "replace")
            cursor += length
            if "=" in entry:
                key, _, value = entry.partition("=")
                tags[key.strip().lower()] = value.strip()
    except (struct.error, IndexError):
        pass
    return tags


_MPEG_BITRATES = {
    # (version_group, layer) -> 14 个码率索引对应的 kbps
    ("1", 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    ("1", 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    ("1", 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    ("2", 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    ("2", 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    ("2", 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_SAMPLE_RATES = {"1": [44100, 48000, 32000], "2": [22050, 24000, 16000], "25": [11025, 12000, 8000]}


def read_mp3(fh, size):
    """MP3：跳过 ID3v2，定位第一个帧头；有 Xing/Info 帧时用帧数算精确时长。"""
    info = {"format": "MP3", "codec": "MPEG Layer III", "channels": None,
            "sample_rate": None, "bit_depth": None, "duration": 0.0, "bitrate": 0,
            "tags": {}}
    start = 0
    id3 = _read_slice(fh, 0, 10)
    if id3[:3] == b"ID3" and len(id3) >= 10:
        tag_size = ((id3[6] & 0x7F) << 21 | (id3[7] & 0x7F) << 14
                    | (id3[8] & 0x7F) << 7 | (id3[9] & 0x7F))
        start = 10 + tag_size
        info["tags"]["id3_version"] = "2.%d" % id3[3]

    frame = _find_mpeg_frame(fh, start, min(size, start + 1 << 20))
    if frame is None:
        raise ContainerError("没有找到 MPEG 帧头 —— 文件可能损坏或不是 MP3")
    offset, header = frame
    version_bits = (header[1] >> 3) & 0x03
    layer_bits = (header[1] >> 1) & 0x03
    bitrate_index = (header[2] >> 4) & 0x0F
    rate_index = (header[2] >> 2) & 0x03
    channel_mode = (header[3] >> 6) & 0x03

    version = {0: "25", 1: None, 2: "2", 3: "1"}.get(version_bits)
    layer = {1: 3, 2: 2, 3: 1}.get(layer_bits)
    if version is None or layer is None:
        raise ContainerError("MPEG 帧头非法（保留位被置位）")
    info["codec"] = "MPEG Layer %s" % {1: "I", 2: "II", 3: "III"}[layer]
    info["channels"] = 1 if channel_mode == 3 else 2
    rates = _SAMPLE_RATES[version]
    info["sample_rate"] = rates[rate_index] if rate_index < len(rates) else None
    table = _MPEG_BITRATES.get((version if version != "25" else "2", layer), [])
    bitrate = table[bitrate_index] if bitrate_index < len(table) else 0
    info["bitrate"] = bitrate * 1000

    # Xing / Info 头：第一帧内含总帧数，据此可算精确时长
    frame_length = _mpeg_frame_length(header, version, layer, bitrate)
    body = _read_slice(fh, offset, max(frame_length, 64))
    frames = _xing_frame_count(body)
    if frames and info["sample_rate"]:
        samples_per_frame = 1152 if layer in (2, 3) or version == "1" else 576
        info["duration"] = frames * samples_per_frame / info["sample_rate"]
        info["bitrate"] = int((size - start) * 8 / info["duration"]) if info["duration"] else 0
    elif info["bitrate"]:
        info["duration"] = (size - start) * 8 / info["bitrate"]
    return info


def _find_mpeg_frame(fh, start, limit):
    """在 ``[start, limit)`` 里找同步字 0xFFE。"""
    fh.seek(start)
    window = fh.read(max(0, limit - start))
    for index in range(len(window) - 4):
        if window[index] == 0xFF and (window[index + 1] & 0xE0) == 0xE0:
            header = window[index:index + 4]
            layer_bits = (header[1] >> 1) & 0x03
            bitrate_index = (header[2] >> 4) & 0x0F
            rate_index = (header[2] >> 2) & 0x03
            if layer_bits in (1, 2, 3) and bitrate_index not in (0, 15) and rate_index != 3:
                return start + index, header
    return None


def _mpeg_frame_length(header, version, layer, bitrate_kbps):
    version_bits = (header[1] >> 3) & 0x03
    rate_index = (header[2] >> 2) & 0x03
    padding = (header[2] >> 1) & 0x01
    rates = _SAMPLE_RATES.get(version, [44100, 48000, 32000])
    rate = rates[rate_index] if rate_index < len(rates) else 44100
    if not bitrate_kbps or not rate:
        return 0
    if layer == 1:
        return int(((12 * bitrate_kbps * 1000 / rate) + padding) * 4)
    samples = 1152 if layer == 3 or version == "1" else 576
    if version == "2" and layer == 3:
        samples = 576
    return int(samples / 8 * bitrate_kbps * 1000 / rate) + padding


def _xing_frame_count(body):
    for marker in (b"Xing", b"Info"):
        index = body.find(marker)
        if index < 0 or index + 8 > len(body):
            continue
        flags = struct.unpack(">I", body[index + 4:index + 8])[0]
        if not (flags & 0x0001):
            return None
        if index + 12 > len(body):
            return None
        return struct.unpack(">I", body[index + 8:index + 12])[0]
    return None


def read_ogg(fh, size):
    """OGG：从第一个数据页的识别头读采样率/声道；时长用最后一页的 granule。"""
    if _read_slice(fh, 0, 4) != b"OggS":
        raise ContainerError("不是合法的 OGG 文件")
    info = {"format": "OGG", "codec": "OGG", "channels": None, "sample_rate": None,
            "bit_depth": None, "duration": 0.0, "bitrate": 0, "tags": {}}

    first = _read_slice(fh, 0, 4096)
    vorbis = first.find(b"\x01vorbis")
    if vorbis >= 0 and vorbis + 16 <= len(first):
        info["codec"] = "Vorbis"
        info["channels"] = first[vorbis + 11]
        info["sample_rate"] = struct.unpack("<I", first[vorbis + 12:vorbis + 16])[0]
    else:
        opus = first.find(b"OpusHead")
        if opus >= 0 and opus + 12 <= len(first):
            info["codec"] = "Opus"
            info["channels"] = first[opus + 9]
            # Opus 的 granule 按 48kHz 计，识别头里的 input rate 仅供参考
            info["sample_rate"] = struct.unpack("<I", first[opus + 12:opus + 16])[0] or 48000
            info["bit_depth"] = 16
        else:
            flac = first.find(b"\x7fFLAC")
            if flac >= 0:
                info["codec"] = "FLAC (in OGG)"
            else:
                info["codec"] = "未知 OGG 编码"

    granule = _last_granule(fh, size)
    if granule and info["sample_rate"]:
        rate = 48000 if info["codec"] == "Opus" else info["sample_rate"]
        info["duration"] = granule / rate
    if info["duration"]:
        info["bitrate"] = int(size * 8 / info["duration"])
    return info


def _last_granule(fh, size):
    """从文件尾部往前找最后一个 ``OggS`` 页，取它的 granule position。"""
    window = min(size, 65536)
    tail = _read_slice(fh, size - window, window)
    index = tail.rfind(b"OggS")
    if index < 0 or index + 14 > len(tail):
        return 0
    return struct.unpack("<q", tail[index + 6:index + 14])[0]


READERS = {
    ".wav": read_wav,
    ".wave": read_wav,
    ".flac": read_flac,
    ".mp3": read_mp3,
    ".ogg": read_ogg,
    ".oga": read_ogg,
    ".opus": read_ogg,
}

AUDIO_EXTS = set(READERS)
MEDIA_EXTS = AUDIO_EXTS | {".m4a", ".mp4", ".mov", ".m4v", ".mkv", ".webm",
                           ".3gp", ".f4v", ".mka"}


def read_audio_metadata(path):
    """读取一个纯音频文件的元数据；不支持的格式抛 :class:`UnsupportedFormat`。"""
    ext = os.path.splitext(path)[1].lower()
    reader = READERS.get(ext)
    size = os.path.getsize(path)
    if reader is None:
        raise UnsupportedFormat(
            "不支持的文件类型：%s（纯音频支持 WAV / FLAC / MP3 / OGG / OPUS）"
            % (ext or "无扩展名")
        )
    with open(path, "rb") as fh:
        info = reader(fh, size)
    info["path"] = path
    info["size"] = size
    for key in ("duration",):
        info[key] = round(float(info.get(key) or 0), 3)
    return info
