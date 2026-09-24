"""合成媒体文件构造器 —— 用于在没有真实样本的情况下验证容器解析。

为什么要合成而不是找样本：本仓库不能把版权媒体文件入库；而自造文件能让每个字节
都可预期，从而把「偏移平移是否正确」这类最容易错的地方**精确断言**出来
（例如抽取出的音轨字节必须与源文件里的音轨字节逐字节相同）。
"""

import io
import struct


def _box(box_type, payload):
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def _full_box(box_type, version, flags, payload):
    return _box(box_type, struct.pack(">B3s", version, flags) + payload)


def _matrix():
    return struct.pack(
        ">9i",
        0x00010000, 0, 0,
        0, 0x00010000, 0,
        0, 0, 0x40000000,
    )


def _sample_sizes(chunk_sizes, samples_per_chunk):
    """把每个块的字节数拆成 ``samples_per_chunk`` 个样本大小。

    ``stsz`` 声明的是**每个样本**的字节数，而抽取器是「样本大小 → 累加成块大小」
    反推块长的，所以这里必须让 k 个样本之和**恰好等于**块的实际长度，
    否则测出来的偏移就对不上（第一版把每个样本写成 1 字节，测试立刻红了）。
    """
    sizes = []
    for size in chunk_sizes:
        if samples_per_chunk <= 1:
            sizes.append(size)
            continue
        base = size // samples_per_chunk
        remainder = size - base * samples_per_chunk
        sizes.extend([base] * (samples_per_chunk - 1))
        sizes.append(base + remainder)
    return sizes


def build_mp4(audio_chunks, video_chunks, audio_codec=b"mp4a", video_codec=b"avc1",
              timescale=1000, samples_per_chunk=1):
    """构造一个双轨（音频 + 视频）的最小合法 MP4。

    :param audio_chunks: 音频块的字节内容列表（每个块 = ``samples_per_chunk`` 个样本）
    :param video_chunks: 视频块的字节内容列表
    :return: 完整的 MP4 字节串

    块在 mdat 里交错排列（音频、视频、音频、视频…），这样 stco 的偏移就**不是**
    简单的连续区间 —— 抽取时若忘了重算偏移，测试立刻会红。
    """
    audio_sizes = [len(chunk) for chunk in audio_chunks]
    video_sizes = [len(chunk) for chunk in video_chunks]

    audio_mdat_offsets = []
    video_mdat_offsets = []
    cursor = 0
    for index in range(max(len(audio_chunks), len(video_chunks))):
        if index < len(audio_chunks):
            audio_mdat_offsets.append(cursor)
            cursor += audio_sizes[index]
        if index < len(video_chunks):
            video_mdat_offsets.append(cursor)
            cursor += video_sizes[index]
    mdat_payload_size = cursor

    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2mp41")

    def build_moov(base_offset):
        mvhd = _full_box(b"mvhd", 0, b"\x00\x00\x00", (
            struct.pack(">II", 0, 0)                       # create / modify
            + struct.pack(">II", timescale, 4000)          # timescale / duration
            + struct.pack(">I", 0x00010000)                # rate
            + struct.pack(">H", 0x0100)                    # volume
            + b"\x00" * 10                                 # reserved
            + _matrix()
            + b"\x00" * 24
            + struct.pack(">I", 3)                         # next_track_id
        ))

        def trak(track_id, handler, codec, chunk_sizes, chunk_offsets, is_audio):
            tkhd = _full_box(b"tkhd", 0, b"\x00\x00\x07", (
                struct.pack(">II", 0, 0)
                + struct.pack(">I", track_id)
                + b"\x00" * 4
                + struct.pack(">I", 4000)
                + b"\x00" * 8
                + struct.pack(">hhhh", 0, 0, 0x0100, 0)
                + _matrix()
                + struct.pack(">II", 640 << 16, 480 << 16)
            ))
            mdhd = _full_box(b"mdhd", 0, b"\x00\x00\x00", (
                struct.pack(">II", 0, 0)
                + struct.pack(">II", timescale, 4000)
                + struct.pack(">HH", 0x55C4, 0)            # 'und'
            ))
            hdlr = _full_box(b"hdlr", 0, b"\x00\x00\x00", (
                b"\x00" * 4 + handler + b"\x00" * 12 + b"Handler\x00"
            ))

            if is_audio:
                # AudioSampleEntry：8 头 + 6 reserved + 2 dri + 2 ver + 2 rev + 4 vendor
                #                 + 2 channels + 2 samplesize + 2 pre + 2 reserved + 4 rate(16.16)
                entry = struct.pack(">I", 8 + 28) + codec + b"\x00" * 6 + struct.pack(">H", 1)
                entry += struct.pack(">HHIHHHHI", 0, 0, 0, 2, 16, 0, 0, 44100 << 16)
                stsd = _full_box(b"stsd", 0, b"\x00\x00\x00",
                                 struct.pack(">I", 1) + entry)
                media_header = _full_box(b"smhd", 0, b"\x00\x00\x00", struct.pack(">HH", 0, 0))
            else:
                entry = struct.pack(">I", 8 + 78) + codec + b"\x00" * 6 + struct.pack(">H", 1)
                entry += b"\x00" * 70
                stsd = _full_box(b"stsd", 0, b"\x00\x00\x00",
                                 struct.pack(">I", 1) + entry)
                media_header = _full_box(b"vmhd", 0, b"\x00\x00\x01", b"\x00" * 8)

            # stts: 一次性覆盖所有样本
            sample_count = len(chunk_sizes) * samples_per_chunk
            stts = _full_box(b"stts", 0, b"\x00\x00\x00",
                             struct.pack(">III", 1, sample_count, 1000 // max(1, sample_count) or 1))
            stsc = _full_box(b"stsc", 0, b"\x00\x00\x00",
                             struct.pack(">IIII", 1, 1, samples_per_chunk, 1))
            per_sample = _sample_sizes(chunk_sizes, samples_per_chunk)
            stsz = _full_box(b"stsz", 0, b"\x00\x00\x00",
                             struct.pack(">II", 0, len(per_sample))
                             + b"".join(struct.pack(">I", value) for value in per_sample))
            stco = _full_box(b"stco", 0, b"\x00\x00\x00",
                             struct.pack(">I", len(chunk_offsets))
                             + b"".join(struct.pack(">I", value) for value in chunk_offsets))

            stbl = _box(b"stbl", stsd + stts + stsc + stsz + stco)
            dinf = _box(b"dinf", _box(b"dref", struct.pack(">II", 0, 0)))
            minf = _box(b"minf", media_header + dinf + stbl)
            mdia = _box(b"mdia", mdhd + hdlr + minf)
            return _box(b"trak", tkhd + mdia)

        audio_offsets = [base_offset + value for value in audio_mdat_offsets]
        video_offsets = [base_offset + value for value in video_mdat_offsets]
        return _box(b"moov", (
            mvhd
            + trak(1, b"soun", audio_codec, audio_sizes, audio_offsets, True)
            + trak(2, b"vide", video_codec, video_sizes, video_offsets, False)
        ))

    # moov 的长度与 stco 的数值无关，所以可以先量后填
    probe = build_moov(0)
    mdat_payload_start = len(ftyp) + len(probe) + 8
    moov = build_moov(mdat_payload_start)

    mdat = struct.pack(">I", mdat_payload_size + 8) + b"mdat"
    payload = bytearray()
    for index in range(max(len(audio_chunks), len(video_chunks))):
        if index < len(audio_chunks):
            payload.extend(audio_chunks[index])
        if index < len(video_chunks):
            payload.extend(video_chunks[index])

    return ftyp + moov + mdat + bytes(payload), audio_mdat_offsets, video_mdat_offsets


# ------------------------------------------------------------------ EBML


def _vint(value, length=None):
    if length is None:
        for candidate in range(1, 9):
            if value < (1 << (7 * candidate)) - 1:
                length = candidate
                break
    out = bytearray(length)
    for index in range(length - 1, -1, -1):
        out[index] = value & 0xFF
        value >>= 8
    out[0] |= 1 << (8 - length)
    return bytes(out)


def _eid(element_id):
    if element_id <= 0xFF:
        return bytes([element_id])
    if element_id <= 0xFFFF:
        return struct.pack(">H", element_id)
    if element_id <= 0xFFFFFF:
        return struct.pack(">I", element_id)[1:]
    return struct.pack(">I", element_id)


def _element(element_id, payload):
    return _eid(element_id) + _vint(len(payload)) + payload


def _uint_element(element_id, value):
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    return _element(element_id, raw)


def build_mkv(audio_frames, video_frames, audio_codec="A_OPUS", video_codec="V_VP9",
              audio_track=1, video_track=2):
    """构造一个双轨（音频 + 视频）的最小合法 MKV。

    每个元素都用**已知长度**写出，且帧数据带可识别的标记，
    这样抽取后可以直接断言「留下来的都是音轨的帧，且字节完全一致」。
    """
    ebml_header = _element(0x1A45DFA3, (
        _uint_element(0x4286, 1)          # EBMLVersion
        + _uint_element(0x42F7, 1)        # EBMLReadVersion
        + _uint_element(0x42F2, 4)        # EBMLMaxIDLength
        + _uint_element(0x42F3, 8)        # EBMLMaxSizeLength
        + _element(0x4282, b"matroska")   # DocType
        + _uint_element(0x4287, 4)        # DocTypeVersion
        + _uint_element(0x4285, 2)        # DocTypeReadVersion
    ))

    info = _element(0x1549A966, (
        _uint_element(0x2AD7B1, 1000000)  # TimestampScale
        + _element(0x4D80, b"synth-writer")
        + _element(0x5741, b"synth-writer")
    ))

    def track_entry(number, track_type, codec, language, channels=None, rate=None):
        payload = _uint_element(0xD7, number) + _uint_element(0x83, track_type)
        payload += _element(0x86, codec.encode())
        payload += _element(0x22B59C, language.encode())
        if channels is not None:
            audio = _uint_element(0x9F, channels) + _element(0xB5, struct.pack(">f", rate))
            payload += _element(0xE1, audio)
        return _element(0xAE, payload)

    tracks = _element(0x1654AE6B, (
        track_entry(audio_track, 2, audio_codec, "und", channels=2, rate=48000.0)
        + track_entry(video_track, 1, video_codec, "und")
    ))

    def simple_block(number, relative, data):
        return _element(0xA3, _vint(number) + struct.pack(">hB", relative, 0x80) + data)

    cluster_count = max(len(audio_frames), len(video_frames))
    clusters = bytearray()
    for index in range(cluster_count):
        body = bytearray()
        body.extend(_uint_element(0xE7, index * 1000))
        if index < len(audio_frames):
            body.extend(simple_block(audio_track, index, audio_frames[index]))
        if index < len(video_frames):
            body.extend(simple_block(video_track, index, video_frames[index]))
        clusters.extend(_element(0x1F43B675, bytes(body)))

    segment = _element(0x18538067, info + tracks + bytes(clusters))
    return ebml_header + segment
