"""图像解码与感知哈希（纯标准库）。

被 ③ 文件查重 与 ⑨ 图片分析 共用。全部自实现 —— 包内不得含预编译二进制
（指引 16.4 一票否决），也不能依赖未预装的运行时（指引 16.5 rank 13）。

支持范围：

===========  ==========================================================
格式          说明
===========  ==========================================================
PNG          完整解码（zlib + 逐行滤波还原），支持位深 8/16、
             颜色类型 0/2/3/4/6、非隔行与 Adam7 隔行
BMP          24/32 位未压缩，以及 8 位调色板
GIF          LZW 解码，取第一帧
JPEG         只解**直流系数**（DC-only），直接得到 1/8 缩放的灰度图 ——
             这正好是感知哈希需要的尺度，比完整解码快一个量级
===========  ==========================================================

关于 JPEG：完整 JPEG 解码在纯 Python 里太慢（一张 1200 万像素照片要几十秒）。
但 DC 系数恰好是每个 8×8 块的均值，把它们按顺序摆好就是一张 1/8 缩放的灰度图。
感知哈希（pHash/dHash/aHash）与「模糊/曝光」这类粗粒度质量判断都只需要这个尺度，
所以 DC-only 既够用又快得多。**分辨率、EXIF、精确清晰度**这类需要全像素的指标
在只解 DC 时会明确标注为「近似值」，不外推。
"""

import re
import struct
import zlib

# ---------------------------------------------------------------- 通用


class ImageError(Exception):
    """解码失败（文件损坏或格式不支持）。"""


class UnsupportedImage(ImageError):
    """格式不在支持范围内。"""


class Gray:
    """一张灰度图：``pixels`` 是 ``width*height`` 的 0..255 字节序列。"""

    __slots__ = ("width", "height", "pixels")

    def __init__(self, width, height, pixels):
        self.width = int(width)
        self.height = int(height)
        self.pixels = bytes(pixels)

    def at(self, x, y):
        return self.pixels[y * self.width + x]

    def box_mean(self, x0, y0, x1, y1):
        """求矩形区域的均值（含 x0..x1-1, y0..y1-1）。"""
        x0 = max(0, min(self.width, x0))
        x1 = max(x0, min(self.width, x1))
        y0 = max(0, min(self.height, y0))
        y1 = max(y0, min(self.height, y1))
        if x1 <= x0 or y1 <= y0:
            return 0
        total = 0
        count = 0
        for y in range(y0, y1):
            row = y * self.width
            for x in range(x0, x1):
                total += self.pixels[row + x]
                count += 1
        return total / count if count else 0

    def resize(self, width, height, sample_step=1):
        """盒式缩放。

        ``sample_step > 1`` 时只抽样部分像素来估算均值 —— 这是为了在不做全像素
        解码的前提下也能得到合理的缩放图（JPEG DC-only 场景）。
        """
        width = max(1, int(width))
        height = max(1, int(height))
        step = max(1, int(sample_step))
        out = bytearray(width * height)
        for target_y in range(height):
            y0 = target_y * self.height // height
            y1 = max(y0 + 1, (target_y + 1) * self.height // height)
            for target_x in range(width):
                x0 = target_x * self.width // width
                x1 = max(x0 + 1, (target_x + 1) * self.width // width)
                total = 0
                count = 0
                for y in range(y0, y1, step):
                    row = y * self.width
                    for x in range(x0, x1, step):
                        total += self.pixels[row + x]
                        count += 1
                out[target_y * width + target_x] = (total // count) if count else 0
        return Gray(width, height, out)


# ---------------------------------------------------------------- PNG

_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def decode_png(data):
    """解码 PNG，返回 :class:`Gray`。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ImageError("不是 PNG 文件（签名不对）")
    pos = 8
    width = height = bit_depth = color_type = interlace = None
    palette = b""
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            if length < 13:
                raise ImageError("PNG 的 IHDR 长度异常")
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", payload[:13])
        elif chunk_type == b"PLTE":
            palette = payload
        elif chunk_type == b"IDAT":
            idat.extend(payload)
        elif chunk_type == b"IEND":
            break
    if width is None:
        raise ImageError("PNG 缺少 IHDR")
    if width <= 0 or height <= 0:
        raise ImageError("PNG 尺寸非法：%dx%d" % (width, height))
    if width * height > 400_000_000:
        raise ImageError("PNG 尺寸过大（%dx%d）" % (width, height))

    raw = zlib.decompress(bytes(idat))
    channels = _PNG_CHANNELS.get(color_type)
    if channels is None:
        raise UnsupportedImage("不支持的 PNG 颜色类型：%d" % color_type)
    if bit_depth not in (1, 2, 4, 8, 16):
        raise UnsupportedImage("不支持的 PNG 位深：%d" % bit_depth)
    if interlace:
        pixels = _png_deinterlace(raw, width, height, bit_depth, channels)
    else:
        pixels = _png_filter(raw, width, height, bit_depth, channels)
    return _png_to_gray(pixels, width, height, bit_depth, color_type, palette)


def _png_filter(raw, width, height, bit_depth, channels):
    """还原 PNG 的逐行滤波（None/Sub/Up/Average/Paeth）。"""
    bits_per_pixel = bit_depth * channels
    stride = (width * bits_per_pixel + 7) // 8
    bpp = max(1, bits_per_pixel // 8)
    out = bytearray(stride * height)
    prev_row = bytearray(stride)
    pos = 0
    for y in range(height):
        if pos >= len(raw):
            raise ImageError("PNG 数据不完整（第 %d 行）" % y)
        filter_type = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        if len(line) < stride:
            raise ImageError("PNG 数据不完整（第 %d 行）" % y)
        pos += stride
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + prev_row[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev_row[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev_row[i]
                c = prev_row[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                nearest = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + nearest) & 0xFF
        else:
            raise ImageError("未知的 PNG 滤波类型：%d" % filter_type)
        out[y * stride:(y + 1) * stride] = line
        prev_row = line
    return out


def _png_deinterlace(raw, width, height, bit_depth, channels):
    """Adam7 隔行还原。"""
    out = bytearray(width * height * channels)
    pos = 0
    for pass_index in range(7):
        start_x = (0, 4, 0, 2, 0, 1, 0)[pass_index]
        start_y = (0, 0, 4, 0, 2, 0, 1)[pass_index]
        step_x = (8, 8, 4, 4, 2, 2, 1)[pass_index]
        step_y = (8, 8, 8, 4, 4, 2, 2)[pass_index]
        pass_width = (width - start_x + step_x - 1) // step_x if width > start_x else 0
        pass_height = (height - start_y + step_y - 1) // step_y if height > start_y else 0
        if pass_width == 0 or pass_height == 0:
            continue
        stride = (pass_width * bit_depth * channels + 7) // 8
        needed = (stride + 1) * pass_height
        block = _png_filter(raw[pos:pos + needed], pass_width, pass_height,
                            bit_depth, channels)
        pos += needed
        for py in range(pass_height):
            for px in range(pass_width):
                src = py * stride + px * channels
                dst_x = start_x + px * step_x
                dst_y = start_y + py * step_y
                if dst_x >= width or dst_y >= height:
                    continue
                dst = (dst_y * width + dst_x) * channels
                out[dst:dst + channels] = block[src:src + channels]
    return out


def _png_to_gray(pixels, width, height, bit_depth, color_type, palette):
    """转灰度。调色板图用加权亮度；低位深按位展开。"""
    out = bytearray(width * height)
    if color_type == 3:
        # 调色板：索引 → RGB → 亮度
        entries = [(palette[i * 3], palette[i * 3 + 1], palette[i * 3 + 2])
                   for i in range(min(256, len(palette) // 3))]
        if not entries:
            entries = [(0, 0, 0)]
        stride = (width * bit_depth + 7) // 8
        for y in range(height):
            for x in range(width):
                index = _read_bits(pixels, y * stride, x, bit_depth)
                r, g, b = entries[index % len(entries)]
                out[y * width + x] = (r * 299 + g * 587 + b * 114) // 1000
        return Gray(width, height, out)

    channels = _PNG_CHANNELS[color_type]
    if bit_depth == 16:
        samples_per_row = width * channels * 2
        for y in range(height):
            row = y * samples_per_row
            for x in range(width):
                base = row + x * channels * 2
                values = [pixels[base + i * 2] for i in range(channels)]
                out[y * width + x] = _luma(values, color_type)
        return Gray(width, height, out)

    stride = (width * bit_depth * channels + 7) // 8
    if bit_depth < 8:
        for y in range(height):
            for x in range(width):
                value = _read_bits(pixels, y * stride, x, bit_depth)
                out[y * width + x] = value * 255 // ((1 << bit_depth) - 1)
        return Gray(width, height, out)

    for y in range(height):
        row = y * width * channels
        for x in range(width):
            base = row + x * channels
            if channels == 1:
                out[y * width + x] = pixels[base]
            else:
                out[y * width + x] = _luma([pixels[base + i] for i in range(channels)],
                                           color_type)
    return Gray(width, height, out)


def _read_bits(data, row_start, index, bit_depth):
    if bit_depth == 8:
        return data[row_start + index]
    per_byte = 8 // bit_depth
    byte_index = row_start + index // per_byte
    if byte_index >= len(data):
        return 0
    shift = 8 - bit_depth * (index % per_byte + 1)
    return (data[byte_index] >> shift) & ((1 << bit_depth) - 1)


def _luma(values, color_type):
    if color_type in (0, 4):
        return values[0]
    r, g, b = values[0], values[1], values[2]
    return (r * 299 + g * 587 + b * 114) // 1000


# ---------------------------------------------------------------- BMP


def decode_bmp(data):
    if data[:2] != b"BM":
        raise ImageError("不是 BMP 文件")
    if len(data) < 54:
        raise ImageError("BMP 文件过短")
    pixel_offset = struct.unpack("<I", data[10:14])[0]
    header_size = struct.unpack("<I", data[14:18])[0]
    if header_size < 40:
        raise UnsupportedImage("只支持 Windows 位图头（BITMAPINFOHEADER 及以上）")
    width, height = struct.unpack("<ii", data[18:26])
    planes, bits = struct.unpack("<HH", data[26:30])
    compression = struct.unpack("<I", data[30:34])[0]
    if planes != 1:
        raise UnsupportedImage("BMP 的 planes 字段必须为 1")
    if compression not in (0, 3):
        raise UnsupportedImage("只支持未压缩与 BI_BITFIELDS 的 BMP（当前 %d）" % compression)
    top_down = height < 0
    height = abs(height)
    if width <= 0 or height <= 0:
        raise ImageError("BMP 尺寸非法")
    if bits not in (8, 24, 32):
        raise UnsupportedImage("只支持 8 / 24 / 32 位 BMP（当前 %d 位）" % bits)

    palette = []
    if bits == 8:
        palette_start = 14 + header_size
        for index in range(256):
            base = palette_start + index * 4
            if base + 3 > len(data):
                break
            b, g, r = data[base], data[base + 1], data[base + 2]
            palette.append((r * 299 + g * 587 + b * 114) // 1000)
        if not palette:
            palette = [0]

    stride = ((width * bits + 31) // 32) * 4
    out = bytearray(width * height)
    for y in range(height):
        src_y = y if top_down else (height - 1 - y)
        row = pixel_offset + src_y * stride
        if row + stride > len(data):
            raise ImageError("BMP 像素数据不完整")
        for x in range(width):
            if bits == 8:
                index = data[row + x]
                out[y * width + x] = palette[index % len(palette)]
            elif bits == 24:
                base = row + x * 3
                b, g, r = data[base], data[base + 1], data[base + 2]
                out[y * width + x] = (r * 299 + g * 587 + b * 114) // 1000
            else:
                base = row + x * 4
                b, g, r = data[base], data[base + 1], data[base + 2]
                out[y * width + x] = (r * 299 + g * 587 + b * 114) // 1000
    return Gray(width, height, out)


# ---------------------------------------------------------------- GIF


def decode_gif(data):
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ImageError("不是 GIF 文件")
    width, height, packed, _bg, _aspect = struct.unpack("<HHBBB", data[6:13])
    if width <= 0 or height <= 0:
        raise ImageError("GIF 尺寸非法")
    pos = 13
    global_table = []
    if packed & 0x80:
        size = 2 << (packed & 0x07)
        global_table, pos = _gif_palette(data, pos, size)

    while pos < len(data):
        block = data[pos]
        pos += 1
        if block == 0x3B:  # trailer
            break
        if block == 0x21:  # extension
            pos += 1
            while pos < len(data) and data[pos]:
                pos += data[pos] + 1
            pos += 1
            continue
        if block != 0x2C:  # 非图像块，跳过
            break
        if pos + 9 > len(data):
            raise ImageError("GIF 图像描述符不完整")
        left, top, frame_w, frame_h, frame_packed = struct.unpack("<HHHHB", data[pos:pos + 9])
        pos += 9
        table = global_table
        if frame_packed & 0x80:
            size = 2 << (frame_packed & 0x07)
            table, pos = _gif_palette(data, pos, size)
        if not table:
            raise ImageError("GIF 缺少调色板")
        pos += 1  # LZW 最小码长
        chunks = bytearray()
        while pos < len(data) and data[pos]:
            length = data[pos]
            pos += 1
            chunks.extend(data[pos:pos + length])
            pos += length
        pos += 1
        indices = _gif_lzw(bytes(chunks), min_code_size=8 if len(table) > 128 else
                           max(2, (len(table).bit_length() - 1)))
        gray = _gif_to_gray(indices, frame_w, frame_h, table)
        if frame_w == width and frame_h == height and left == 0 and top == 0:
            return gray
        # 只取第一帧，尺寸不一致时裁到画布范围
        canvas = Gray(width, height, bytes(width * height))
        return _gif_blit(canvas, gray, left, top)
    raise ImageError("GIF 里没有找到图像数据")


def _gif_palette(data, pos, size):
    table = []
    for index in range(size):
        base = pos + index * 3
        if base + 3 > len(data):
            break
        r, g, b = data[base], data[base + 1], data[base + 2]
        table.append((r * 299 + g * 587 + b * 114) // 1000)
    return table, pos + size * 3


def _gif_lzw(data, min_code_size):
    """GIF 的 LZW 解码。"""
    clear_code = 1 << min_code_size
    end_code = clear_code + 1
    code_size = min_code_size + 1
    dictionary = [bytes([i]) for i in range(clear_code)] + [b"", b""]
    out = bytearray()
    bit_buffer = 0
    bit_count = 0
    previous = None
    for byte in data:
        bit_buffer |= byte << bit_count
        bit_count += 8
        while bit_count >= code_size:
            code = bit_buffer & ((1 << code_size) - 1)
            bit_buffer >>= code_size
            bit_count -= code_size
            if code == clear_code:
                dictionary = [bytes([i]) for i in range(clear_code)] + [b"", b""]
                code_size = min_code_size + 1
                previous = None
                continue
            if code == end_code:
                return bytes(out)
            if code < len(dictionary):
                entry = dictionary[code]
            elif previous is not None:
                entry = previous + previous[:1]
            else:
                break
            out.extend(entry)
            if previous is not None:
                dictionary.append(previous + entry[:1])
                if len(dictionary) == (1 << code_size) and code_size < 12:
                    code_size += 1
            previous = entry
    return bytes(out)


def _gif_to_gray(indices, width, height, table):
    out = bytearray(width * height)
    limit = width * height
    for i in range(min(len(indices), limit)):
        out[i] = table[indices[i] % len(table)]
    return Gray(width, height, out)


def _gif_blit(canvas, source, left, top):
    pixels = bytearray(canvas.pixels)
    for y in range(source.height):
        target_y = top + y
        if target_y >= canvas.height:
            break
        for x in range(source.width):
            target_x = left + x
            if target_x >= canvas.width:
                break
            pixels[target_y * canvas.width + target_x] = source.at(x, y)
    return Gray(canvas.width, canvas.height, pixels)


# ---------------------------------------------------------------- JPEG（DC-only）


_JPEG_ZIGZAG_DC = 0  # DC 系数在量化表与熵编码里都排在第一个


def decode_jpeg_dc(data):
    """基线 JPEG 的 DC-only 解码，返回 1/8 缩放的灰度图。

    只解直流系数：每个 8×8 块取 huffman 解出的第一个系数，乘量化表首项，
    得到该块的平均值（相对 128 的偏移）。把块按顺序排好即为 1/8 缩放的灰度图。
    """
    if data[:2] != b"\xff\xd8":
        raise ImageError("不是 JPEG 文件（缺少 SOI）")
    pos = 2
    quant_tables = {}
    huffman_tables = {}
    frame = None
    scan = None
    restart_interval = 0

    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9:
            break
        if pos + 2 > len(data):
            break
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        segment = data[pos + 2:pos + length]
        pos += length
        if marker == 0xDB:
            _parse_quant_tables(segment, quant_tables)
        elif marker == 0xC4:
            _parse_huffman_tables(segment, huffman_tables)
        elif marker in (0xC0, 0xC1):
            frame = _parse_sof(segment)
        elif marker == 0xC2:
            raise UnsupportedImage("渐进式 JPEG（SOF2）不支持 DC-only 快速解码")
        elif marker == 0xDD:
            restart_interval = struct.unpack(">H", segment[:2])[0] if len(segment) >= 2 else 0
        elif marker == 0xDA:
            scan = _parse_sos(segment)
            break
        elif marker in (0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            raise UnsupportedImage("不支持的 JPEG 编码类型（标记 0x%02X）" % marker)

    if frame is None or scan is None:
        raise ImageError("JPEG 缺少 SOF 或 SOS 段")

    entropy = data[pos:]
    return _jpeg_decode_dc(entropy, frame, scan, quant_tables, huffman_tables,
                           restart_interval)


def _parse_quant_tables(segment, out):
    pos = 0
    while pos < len(segment):
        spec = segment[pos]
        precision = spec >> 4
        table_id = spec & 0x0F
        pos += 1
        if precision:
            if pos + 128 > len(segment):
                break
            values = list(struct.unpack(">64H", segment[pos:pos + 128]))
            pos += 128
        else:
            if pos + 64 > len(segment):
                break
            values = list(segment[pos:pos + 64])
            pos += 64
        out[table_id] = values


_HUFF_ORDER = list(range(64))  # 占位：量化表按 zigzag 顺序存放，DC 恒为第 0 项


def _parse_huffman_tables(segment, out):
    pos = 0
    while pos < len(segment):
        spec = segment[pos]
        table_class = spec >> 4
        table_id = spec & 0x0F
        pos += 1
        if pos + 16 > len(segment):
            break
        counts = list(segment[pos:pos + 16])
        pos += 16
        total = sum(counts)
        symbols = list(segment[pos:pos + total])
        pos += total
        out[(table_class, table_id)] = _build_huffman(counts, symbols)


def _build_huffman(counts, symbols):
    """由码长表建 (code, length) → symbol 的查找表。"""
    table = {}
    code = 0
    index = 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            if index < len(symbols):
                table[(code, length)] = symbols[index]
                index += 1
            code += 1
        code <<= 1
    return table


class _BitReader:
    __slots__ = ("data", "pos", "buffer", "count", "_marker_seen")

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.buffer = 0
        self.count = 0

    def bit(self):
        if self.count == 0:
            if self.pos >= len(self.data):
                return None
            byte = self.data[self.pos]
            self.pos += 1
            if byte == 0xFF:
                # 字节填充：0xFF 后面跟 0x00 表示数据里的 0xFF
                if self.pos < len(self.data) and self.data[self.pos] == 0x00:
                    self.pos += 1
                else:
                    return None  # 遇到标记，扫描数据结束
            self.buffer = byte
            self.count = 8
        self.count -= 1
        return (self.buffer >> self.count) & 1

    def bits(self, count):
        value = 0
        for _ in range(count):
            bit = self.bit()
            if bit is None:
                return None
            value = (value << 1) | bit
        return value

    def huffman(self, table):
        code = 0
        for length in range(1, 17):
            bit = self.bit()
            if bit is None:
                return None
            code = (code << 1) | bit
            symbol = table.get((code, length))
            if symbol is not None:
                return symbol
        return None

    def align(self):
        self.count = 0


def _parse_sof(segment):
    if len(segment) < 6:
        raise ImageError("SOF 段过短")
    precision, height, width, components = struct.unpack(">BHHB", segment[:6])
    if precision != 8:
        raise UnsupportedImage("只支持 8 位精度 JPEG（当前 %d 位）" % precision)
    comps = []
    pos = 6
    for _ in range(components):
        if pos + 3 > len(segment):
            break
        comp_id, sampling, quant_id = struct.unpack(">BBB", segment[pos:pos + 3])
        comps.append({"id": comp_id, "h": sampling >> 4, "v": sampling & 0x0F,
                      "quant": quant_id})
        pos += 3
    return {"precision": precision, "width": width, "height": height,
            "components": comps}


def _parse_sos(segment):
    if len(segment) < 1:
        raise ImageError("SOS 段过短")
    components = segment[0]
    comps = []
    pos = 1
    for _ in range(components):
        if pos + 2 > len(segment):
            break
        comp_id, tables = struct.unpack(">BB", segment[pos:pos + 2])
        comps.append({"id": comp_id, "dc": tables >> 4, "ac": tables & 0x0F})
        pos += 2
    return {"components": comps}


def _extend(value, bits):
    """JPEG 的符号扩展。"""
    if bits == 0:
        return 0
    if value < (1 << (bits - 1)):
        return value - (1 << bits) + 1
    return value


def _jpeg_decode_dc(data, frame, scan, quant_tables, huffman_tables, restart_interval):
    """按 MCU 顺序取每个块的 DC 系数，拼成 1/8 缩放的灰度图。"""
    components = frame["components"]
    if not components:
        raise ImageError("JPEG 没有分量")
    h_max = max(comp["h"] for comp in components)
    v_max = max(comp["v"] for comp in components)
    mcu_width = 8 * h_max
    mcu_height = 8 * v_max
    mcus_x = (frame["width"] + mcu_width - 1) // mcu_width
    mcus_y = (frame["height"] + mcu_height - 1) // mcu_height

    # 各分量的 DC 预测值与量化步长
    state = {}
    for comp in components:
        table = quant_tables.get(comp["quant"])
        if not table:
            raise ImageError("JPEG 缺少量化表 %d" % comp["quant"])
        state[comp["id"]] = {"pred": 0, "quant": table[0],
                             "h": comp["h"], "v": comp["v"]}

    scan_tables = {}
    for comp in scan["components"]:
        scan_tables[comp["id"]] = comp["dc"]

    reader = _BitReader(data)
    blocks = {}
    mcu_count = 0
    for mcu_y in range(mcus_y):
        for mcu_x in range(mcus_x):
            if restart_interval and mcu_count and mcu_count % restart_interval == 0:
                reader.align()
                # 跳过 RSTn 标记
                for _ in range(4):
                    if reader.pos < len(reader.data):
                        reader.pos += 1
                    else:
                        break
                reader.count = 0
                for comp in components:
                    state[comp["id"]]["pred"] = 0
            mcu_count += 1
            for comp in components:
                info = state[comp["id"]]
                dc_table_id = scan_tables.get(comp["id"])
                table = huffman_tables.get((0, dc_table_id))
                if table is None:
                    # 没有 DC 表的退化情形：整块视为中性灰
                    for by in range(info["v"]):
                        for bx in range(info["h"]):
                            blocks[(comp["id"], mcu_x * info["h"] + bx,
                                    mcu_y * info["v"] + by)] = 128
                    continue
                for by in range(info["v"]):
                    for bx in range(info["h"]):
                        length = reader.huffman(table)
                        if length is None:
                            raise ImageError("JPEG 熵编码数据不完整")
                        if length:
                            raw = reader.bits(length)
                            if raw is None:
                                raise ImageError("JPEG 熵编码数据不完整")
                            diff = _extend(raw, length)
                        else:
                            diff = 0
                        info["pred"] += diff
                        value = info["pred"] * info["quant"] / 8.0 + 128
                        blocks[(comp["id"], mcu_x * info["h"] + bx,
                                mcu_y * info["v"] + by)] = max(0, min(255, int(value)))
                        # 跳过该块的 63 个 AC 系数（我们用不到，但必须扫过去）
                        if not _skip_ac(reader, huffman_tables.get((1, scan_tables_ac(scan, comp["id"])))):
                            raise ImageError("JPEG 熵编码数据不完整（AC 扫描中断）")

    # 用亮度分量（通常是第一个）拼出 1/8 缩放图
    luma = components[0]
    info = state[luma["id"]]
    grid_w = mcus_x * info["h"]
    grid_h = mcus_y * info["v"]
    out = bytearray(grid_w * grid_h)
    for gy in range(grid_h):
        for gx in range(grid_w):
            out[gy * grid_w + gx] = blocks.get((luma["id"], gx, gy), 128)
    return Gray(grid_w, grid_h, out)


def scan_tables_ac(scan, comp_id):
    for comp in scan["components"]:
        if comp["id"] == comp_id:
            return comp["ac"]
    return 0


def _skip_ac(reader, table):
    """跳过剩余 63 个 AC 系数。"""
    if table is None:
        return True
    for _ in range(63):
        symbol = reader.huffman(table)
        if symbol is None:
            return False
        run = symbol >> 4
        size = symbol & 0x0F
        if size == 0:
            if run == 15:
                continue  # ZRL：跳过 16 个零
            break  # EOB
        if reader.bits(size) is None:
            return False
        for _ in range(run):
            pass
    return True


# ---------------------------------------------------------------- 统一入口

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def sniff(data):
    if data[:8] == PNG_SIG:
        return "png"
    if data[:2] == b"BM":
        return "bmp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    return None


SUPPORTED_EXTS = {".png", ".bmp", ".gif", ".jpg", ".jpeg"}
SUPPORTED_FORMATS = {"png", "bmp", "gif", "jpeg"}


def decode_file(path, max_pixels=400_000_000):
    """解码一个图片文件，返回 ``(Gray, info)``。

    ``info`` 带 ``format`` / ``full_decode`` 两个字段：``full_decode=False`` 表示
    走的是 JPEG 的 DC-only 通道，此时图是 1/8 缩放的，全像素指标不外推。
    """
    import os

    with open(path, "rb") as fh:
        data = fh.read()
    if not data:
        raise ImageError("文件为空")
    fmt = sniff(data)
    if fmt is None:
        raise UnsupportedImage("不支持的图片格式：%s" % (os.path.splitext(path)[1] or "未知"))
    if fmt == "png":
        return decode_png(data), {"format": "PNG", "full_decode": True}
    if fmt == "bmp":
        return decode_bmp(data), {"format": "BMP", "full_decode": True}
    if fmt == "gif":
        return decode_gif(data), {"format": "GIF", "full_decode": True}
    return decode_jpeg_dc(data), {"format": "JPEG", "full_decode": False}


# ---------------------------------------------------------------- 感知哈希


def _dct_matrix(size):
    """预计算 DCT-II 的余弦矩阵：``matrix[u][x] = cos((2x+1)uπ / 2N)``。"""
    import math

    return [[math.cos((2 * x + 1) * u * math.pi / (2 * size)) for x in range(size)]
            for u in range(size)]


_DCT32 = _dct_matrix(32)


def phash(image, hash_size=8, high_size=32):
    """pHash：DCT 后取左上角低频块，与中位数比较得到位串。

    返回 ``(hex_string, bits_int, bit_count)``。
    """
    small = image.resize(high_size, high_size)
    pixels = small.pixels
    # 可分离 DCT：先按行做，再按列做，避免 O(N^4)
    rows = []
    for y in range(high_size):
        row = pixels[y * high_size:(y + 1) * high_size]
        rows.append([sum(_DCT32[u][x] * row[x] for x in range(high_size))
                     for u in range(high_size)])
    coeffs = []
    for u in range(high_size):
        column = [rows[y][u] for y in range(high_size)]
        coeffs.append([sum(_DCT32[v][y] * column[y] for y in range(high_size))
                       for v in range(high_size)])

    low = []
    for v in range(hash_size):
        for u in range(hash_size):
            if v == 0 and u == 0:
                continue  # 跳过直流项
            low.append(coeffs[v][u])
    if not low:
        return "0" * 16, 0, hash_size * hash_size
    ordered = sorted(low)
    median = ordered[len(ordered) // 2]
    bits = 0
    for value in low:
        bits = (bits << 1) | (1 if value > median else 0)
    return "%016x" % bits, bits, len(low)


def dhash(image, hash_size=8):
    """dHash：缩到 (N+1)×N，比较水平相邻像素。"""
    small = image.resize(hash_size + 1, hash_size)
    bits = 0
    for y in range(hash_size):
        row = y * (hash_size + 1)
        for x in range(hash_size):
            left = small.pixels[row + x]
            right = small.pixels[row + x + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return "%016x" % bits, bits, hash_size * hash_size


def ahash(image, hash_size=8):
    """aHash：缩到 N×N，与均值比较。"""
    small = image.resize(hash_size, hash_size)
    values = list(small.pixels)
    mean = sum(values) / len(values) if values else 0
    bits = 0
    for value in values:
        bits = (bits << 1) | (1 if value > mean else 0)
    return "%016x" % bits, bits, len(values)


def hamming(a, b):
    """两个位串的汉明距离。"""
    return bin(a ^ b).count("1")


def all_hashes(image):
    """一次算出三种哈希（避免重复缩放）。"""
    phash_hex, phash_bits, phash_n = phash(image)
    dhash_hex, dhash_bits, dhash_n = dhash(image)
    ahash_hex, ahash_bits, ahash_n = ahash(image)
    return {
        "phash": phash_hex, "dhash": dhash_hex, "ahash": ahash_hex,
        "bits": {"phash": phash_bits, "dhash": dhash_bits, "ahash": ahash_bits},
        "sizes": {"phash": phash_n, "dhash": dhash_n, "ahash": ahash_n},
    }


# ---------------------------------------------------------------- 质量指标


def laplacian_variance(image, sample_step=1):
    """拉普拉斯方差 —— 数值越低越模糊。

    在 1/8 缩放的图上计算时，绝对值与全分辨率不可比，但**同一批图之间的相对排序**
    仍然有效（都是同一尺度），所以用它排序选最佳照片是可靠的；界面上会标注为近似值。
    """
    width, height = image.width, image.height
    if width < 3 or height < 3:
        return 0.0
    step = max(1, int(sample_step))
    values = []
    pixels = image.pixels
    for y in range(1, height - 1, step):
        base = y * width
        prev = base - width
        nxt = base + width
        for x in range(1, width - 1, step):
            lap = (4 * pixels[base + x]
                   - pixels[base + x - 1] - pixels[base + x + 1]
                   - pixels[prev + x] - pixels[nxt + x])
            values.append(lap)
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


def brightness_stats(image):
    """亮度直方图统计：均值、标准差、过暗/过曝比例。"""
    pixels = image.pixels
    total = len(pixels)
    if not total:
        return {"mean": 0.0, "stdev": 0.0, "dark_ratio": 0.0, "bright_ratio": 0.0,
                "histogram": [0] * 16}
    histogram = [0] * 16
    total_value = 0
    for value in pixels:
        histogram[value >> 4] += 1
        total_value += value
    mean = total_value / total
    variance = 0.0
    for value in pixels:
        variance += (value - mean) ** 2
    dark = sum(1 for value in pixels if value < 32) / total
    bright = sum(1 for value in pixels if value > 240) / total
    return {
        "mean": round(mean, 2),
        "stdev": round((variance / total) ** 0.5, 2),
        "dark_ratio": round(dark, 4),
        "bright_ratio": round(bright, 4),
        "histogram": histogram,
    }


def edge_density(image, threshold=24, sample_step=1):
    """粗略的边缘密度：相邻像素差超过阈值的比例。"""
    width, height = image.width, image.height
    if width < 2 or height < 2:
        return 0.0
    step = max(1, int(sample_step))
    edges = 0
    total = 0
    pixels = image.pixels
    for y in range(0, height, step):
        base = y * width
        for x in range(1, width, step):
            if abs(pixels[base + x] - pixels[base + x - 1]) > threshold:
                edges += 1
            total += 1
    return round(edges / total, 4) if total else 0.0


def assess_quality(image, full_decode=True):
    """给出一张图的粗粒度质量判断。

    ``sharpness`` 在 DC-only 图上按 1/8 尺度计算，因此只在**同批次之间**可比；
    返回结构里带 ``approximate`` 标记，界面据此说明。
    """
    brightness = brightness_stats(image)
    sharpness = laplacian_variance(image)
    issues = []
    if sharpness < 60:
        issues.append("可能模糊")
    if brightness["mean"] < 45 or brightness["dark_ratio"] > 0.55:
        issues.append("偏暗")
    if brightness["mean"] > 215 or brightness["bright_ratio"] > 0.45:
        issues.append("过曝")
    if brightness["stdev"] < 22:
        issues.append("对比度低")
    if image.width < 160 or image.height < 120:
        issues.append("分辨率低")
    return {
        "sharpness": round(sharpness, 1),
        "brightness": brightness["mean"],
        "contrast": brightness["stdev"],
        "dark_ratio": brightness["dark_ratio"],
        "bright_ratio": brightness["bright_ratio"],
        "histogram": brightness["histogram"],
        "edge_density": edge_density(image),
        "decoded_size": [image.width, image.height],
        "approximate": not full_decode,
        "issues": issues,
        "ok": not issues,
    }


def quality_score(quality):
    """把多项指标合成一个 0..100 的排序分。

    刻意**只用于排序**，界面上同时展示各项分指标与命中的问题 —— 设计文档 §16.5
    明确要求「不要输出一个 AI 分数作为最终结果」。
    """
    sharp = min(100.0, quality["sharpness"] / 6.0)
    exposure_penalty = 0.0
    mean = quality["brightness"]
    if mean < 60:
        exposure_penalty += (60 - mean) * 1.2
    elif mean > 200:
        exposure_penalty += (mean - 200) * 1.2
    exposure_penalty += quality["dark_ratio"] * 60 + quality["bright_ratio"] * 60
    contrast = min(100.0, quality["contrast"] * 2.2)
    score = 0.5 * sharp + 0.3 * contrast - 0.4 * exposure_penalty + 20
    return round(max(0.0, min(100.0, score)), 1)


# ---------------------------------------------------------------- 尺寸与 EXIF

_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_dimensions(data):
    """从 JPEG 的 SOF 段读宽高（不解像素）。"""
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > len(data):
            break
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        if marker in _SOF_MARKERS and length >= 7:
            height, width = struct.unpack(">HH", data[pos + 3:pos + 7])
            return width, height
        if marker == 0xDA:
            break
        pos += length
    return None, None


def read_exif(data):
    """读 JPEG 里的 EXIF 摘要字段（不引第三方库）。"""
    result = {}
    if data[:2] != b"\xff\xd8":
        return result
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker == 0xE1:
            length = struct.unpack(">H", data[pos:pos + 2])[0]
            segment = data[pos + 2:pos + length]
            if segment[:6] == b"Exif\x00\x00":
                result.update(_parse_tiff(segment[6:]))
            return result
        if marker in (0xDA, 0xD9):
            break
        if pos + 2 > len(data):
            break
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        pos += length
    return result


_EXIF_TAGS = {
    0x010F: "make", 0x0110: "model", 0x0112: "orientation", 0x011A: "x_resolution",
    0x0132: "datetime", 0x829A: "exposure_time", 0x829D: "f_number",
    0x8827: "iso", 0x9003: "datetime_original", 0x920A: "focal_length",
    0xA002: "pixel_x", 0xA003: "pixel_y", 0x011B: "y_resolution", 0x0128: "resolution_unit",
}


def _parse_tiff(block):
    tags = {}
    if len(block) < 8:
        return tags
    endian = block[:2]
    if endian == b"II":
        order = "<"
    elif endian == b"MM":
        order = ">"
    else:
        return tags
    try:
        offset = struct.unpack(order + "I", block[4:8])[0]
        if offset + 2 > len(block):
            return tags
        count = struct.unpack(order + "H", block[offset:offset + 2])[0]
        for index in range(min(count, 128)):
            base = offset + 2 + index * 12
            if base + 12 > len(block):
                break
            tag, field_type, field_count = struct.unpack(order + "HHI", block[base:base + 8])
            name = _EXIF_TAGS.get(tag)
            if not name:
                continue
            value = _read_exif_value(block, base + 8, order, field_type, field_count)
            if value is not None:
                tags[name] = value
    except (struct.error, IndexError):
        pass
    return tags


def _read_exif_value(block, pos, order, field_type, field_count):
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
    size = sizes.get(field_type)
    if size is None:
        return None
    total = size * field_count
    if total > 4:
        if pos + 4 > len(block):
            return None
        offset = struct.unpack(order + "I", block[pos:pos + 4])[0]
        if offset + total > len(block):
            return None
        raw = block[offset:offset + total]
    else:
        raw = block[pos:pos + total]
    try:
        if field_type == 2:
            return raw.rstrip(b"\x00").decode("utf-8", "replace")
        if field_type == 3:
            return struct.unpack(order + "%dH" % field_count, raw[:2 * field_count])[0]
        if field_type in (4, 9):
            return struct.unpack(order + "%dI" % field_count, raw[:4 * field_count])[0]
        if field_type in (5, 10):
            num, den = struct.unpack(order + "II", raw[:8])
            return round(num / den, 4) if den else None
    except (struct.error, ZeroDivisionError):
        return None
    return None


def image_metadata(path):
    """不解码像素的元数据：格式、尺寸、EXIF。"""
    import os

    with open(path, "rb") as fh:
        head = fh.read(1 << 20)
    fmt = sniff(head)
    if fmt is None:
        raise UnsupportedImage("不支持的图片格式：%s" % (os.path.splitext(path)[1] or "未知"))
    meta = {"path": path, "size": os.path.getsize(path), "format": fmt.upper()}
    if fmt == "png" and len(head) >= 24:
        width, height = struct.unpack(">II", head[16:24])
        meta.update({"width": width, "height": height, "bit_depth": head[24],
                     "color_type": head[25]})
    elif fmt == "bmp" and len(head) >= 26:
        width, height = struct.unpack("<ii", head[18:26])
        meta.update({"width": abs(width), "height": abs(height), "bit_depth":
                     struct.unpack("<H", head[28:30])[0]})
    elif fmt == "gif" and len(head) >= 10:
        width, height = struct.unpack("<HH", head[6:10])
        meta.update({"width": width, "height": height})
    elif fmt == "jpeg":
        width, height = jpeg_dimensions(head)
        meta.update({"width": width, "height": height, "exif": read_exif(head)})
    meta["pixels"] = (meta.get("width") or 0) * (meta.get("height") or 0)
    return meta


def is_supported_name(name):
    import os

    return os.path.splitext(name)[1].lower() in SUPPORTED_EXTS


def safe_int(value, default=0):
    try:
        return int(re.sub(r"[^0-9-]", "", str(value)) or default)
    except (TypeError, ValueError):
        return default
