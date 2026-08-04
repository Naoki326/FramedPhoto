"""esptouch.py — ESPTouch v1 (SmartConfig) 发送端。

移植自乐鑫 ESP-Touch 协议（v1，无加密）。设备端 esp_smartconfig
（SC_TYPE_ESPTOUCH）在未关联 WiFi 时也能监听这些 UDP 包，
因此可以在不打断任何设备网络的前提下完成配网。

协议要点（v1）：
- 目标：UDP 组播 234.x.x.x:7001（或广播 255.255.255.255:7001）
- Guide 阶段：4 个引导包（长度 512..515），约 2 秒
- Data 阶段：每个字节编码为 3 个包（9bit 码 + CRC），约 4 秒
- 数据 = 本机 IP(4B) + 密码 + SSID，另有 5 字节头部校验
"""
import socket
import time
import logging
import os
import struct
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding

logger = logging.getLogger(__name__)

_PORT = 7001
_SEND_INTERVAL_S = 0.008          # 8ms 一包
_GUIDE_DURATION_S = 2.0
_DATA_DURATION_S = 4.0


class _Buffer:
    """长度编码发送缓冲：包内容无关紧要，长度即信息。"""

    def __init__(self, broadcast: bool = False):
        self.buf = bytearray(600)
        self.data_to_send: list[int] = []
        self.address_count = 0
        self.broadcast = broadcast

    def _next_target(self) -> tuple[str, int]:
        if self.broadcast:
            return ("255.255.255.255", _PORT)
        self.address_count += 1
        addr = f"234.{self.address_count % 100}.{self.address_count % 100}.{self.address_count % 100}"
        return (addr, _PORT)

    def _send_packet(self, sock: socket.socket, dest: tuple, size: int) -> None:
        sock.sendto(self.buf[0:size], dest)

    def _make_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self.broadcast:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        return sock


def _add_to_crc(b: int, crc: int) -> int:
    if b < 0:
        b += 256
    for _ in range(8):
        odd = ((b ^ crc) & 1) == 1
        crc >>= 1
        b >>= 1
        if odd:
            crc ^= 0x8C
    return crc


def _encode_data_byte(data_byte: int, seq: int) -> tuple[int, int, int]:
    """单字节 → 3 个包长度（9bit 编码：crc 高/低半字节 + 数据半字节 + 40 偏移）。"""
    crc = _add_to_crc(data_byte, 0)
    crc = _add_to_crc(seq, crc)
    first = ((crc >> 4) << 4 | (data_byte >> 4)) + 40
    second = 296 + seq           # 9bit 控制位（0x1xx）+ seq
    third = ((crc & 0x0F) << 4 | (data_byte & 0x0F)) + 40
    return (first, second, third)


def _prepare(ssid: bytes, password: bytes, ip: bytes, bssid: bytes = b"") -> tuple[list[int], int]:
    """组装发送序列：返回 (包长度序列, guide code 数量)。"""
    total_data_length = 5 + len(ip) + len(password) + len(ssid)
    ssid_crc = 0
    for b in ssid:
        ssid_crc = _add_to_crc(b, ssid_crc)
    bssid_crc = 0
    for b in bssid:
        bssid_crc = _add_to_crc(b, bssid_crc)
    total_xor = total_data_length ^ len(password) ^ ssid_crc ^ bssid_crc
    for b in (ip + password + ssid):
        total_xor ^= b

    datum = (total_data_length, len(password), ssid_crc, bssid_crc, total_xor)
    payload = ip + password + ssid

    seqs: list[int] = []
    seq = 0
    for d in datum:
        for p in _encode_data_byte(d, seq):
            seqs.append(p)
        seq += 1

    i_bssid = len(datum)
    bssid_index = 0
    payload_index = 0
    for d in payload:
        if payload_index % 4 == 0 and bssid_index < len(bssid):
            for p in _encode_data_byte(bssid[bssid_index], i_bssid):
                seqs.append(p)
            i_bssid += 1
            bssid_index += 1
        for p in _encode_data_byte(d, seq):
            seqs.append(p)
        seq += 1
        payload_index += 1
    while bssid_index < len(bssid):
        for p in _encode_data_byte(bssid[bssid_index], i_bssid):
            seqs.append(p)
        i_bssid += 1
        bssid_index += 1
    return seqs


def send_smartconfig(ssid: str, password: str, ip: str | None = None,
                     bssid: str = "", duration_s: float | None = None,
                     broadcast: bool = False) -> dict:
    """发送 ESPTouch v1 配网广播。

    :param ssid:     目标 WiFi SSID（UTF-8）
    :param password: WiFi 密码
    :param ip:       发送方 IP（可选，默认取本机内网 IP；协议要求 4 字节）
    :param bssid:    可选 BSSID（hex 字符串，如 "aabbccddeeff"）
    :param duration_s: 发送时长（默认 6s：guide 2s + data 4s）
    :param broadcast: True 用 255.255.255.255 广播，False 用 234.x 组播
    :return: {"sent": True, "packets": n}
    """
    if not ssid:
        raise ValueError("ssid required")
    if ip is None:
        ip = _local_ip()
    ip_bytes = bytes(int(x) for x in ip.split("."))
    if len(ip_bytes) != 4:
        raise ValueError(f"invalid ip {ip}")

    bssid_bytes = bytes.fromhex(bssid) if bssid else b""
    ssid_bytes = ssid.encode("utf-8")
    pass_bytes = password.encode("utf-8")

    data_seqs = _prepare(ssid_bytes, pass_bytes, ip_bytes, bssid_bytes)
    if not data_seqs:
        raise ValueError("empty payload")

    buf = _Buffer(broadcast=broadcast)
    sock = buf._make_socket()
    try:
        # 循环发送 guide + data：设备找到信道后可能错过首轮数据，
        # 乐鑫 App 同样循环广播直到设备配网成功（本实现固定时长）。
        total_s = duration_s or 30.0
        guide = (515, 514, 513, 512)
        packets = 0
        end = time.monotonic() + total_s
        while time.monotonic() < end:
            # Guide：4 个引导包轮发 2 秒
            index = 0
            next_time = time.monotonic()
            g_end = min(time.monotonic() + _GUIDE_DURATION_S, end)
            while time.monotonic() < g_end or index != 0:
                now = time.monotonic()
                if now >= next_time:
                    buf._send_packet(sock, buf._next_target(), guide[index])
                    next_time = now + _SEND_INTERVAL_S
                    index = (index + 1) % 4

            # Data：包长度序列轮发
            index = 0
            next_time = time.monotonic()
            d_end = min(time.monotonic() + _DATA_DURATION_S, end)
            while time.monotonic() < d_end or index != 0:
                now = time.monotonic()
                if now >= next_time:
                    buf._send_packet(sock, buf._next_target(), data_seqs[index])
                    next_time = now + _SEND_INTERVAL_S
                    packets += 1
                    index = (index + 1) % len(data_seqs)
    finally:
        sock.close()

    logger.info("smartconfig sent: ssid=%s packets=%d (%.0fs)", ssid, packets, total_s)
    return {"sent": True, "packets": packets, "ssid": ssid}


def _local_ip() -> str:
    """取本机默认路由的内网 IP。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "0.0.0.0"


# =====================================================================
# ESPTouch v2（AES 加密，共享密钥模式）
#
# 移植自 EspressifApp/EsptouchForAndroid 的 esptouch-v2（EspProvisioningParams
# + TouchAES + TouchCRC + TouchPacketUtils）。与 v1 完全不同的算法：
#   - 数据流 = head(6) + 加密password(pad 5) + aesIV(20) + ssid(pad 6)
#   - 每 6 字节切成 8 个 UDP 包（bit 切片，包长度 = (idx<<7)|(1<<6)|data）
#   - AES-128-CBC，key 为发送端/设备端共享的 16 字节密钥，随机 IV 随包传输
#   - 广播 255.255.255.255:7001，15ms/包循环发送
# =====================================================================

_V2_DEVICE_PORT = 7001
_V2_INTERVAL_S = 0.015
_V2_SSID_PAD = 6
_V2_PWD_PAD = 5          # 加密后密码的 padding 因子
_V2_SEQUENCE_FIRST = -1


class _V2CRC:
    """ESPTouch v2 CRC8：poly 0x8c，init 0x00（与 v1 的 bit-CRC 不同）"""

    def __init__(self):
        self.table = [0] * 256
        for dividend in range(256):
            rem = dividend
            for _ in range(8):
                if rem & 1:
                    rem = (rem >> 1) ^ 0x8C
                else:
                    rem >>= 1
            self.table[dividend] = rem
        self.value = 0

    def update(self, data: bytes) -> None:
        for b in data:
            d = b ^ self.value
            self.value = (self.table[d & 0xFF] ^ (self.value << 8)) & 0xFF

    def reset(self) -> None:
        self.value = 0

    def get(self) -> int:
        return self.value & 0xFF


def _v2_encrypt_aes_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC + PKCS7（Android 用 PKCS5，对 16B block 等价）"""
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv[:16]))
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def _v2_rand_bytes(rng, n: int) -> bytes:
    return bytes(rng.randrange(127) for _ in range(n))


class _V2PacketBuilder:
    """v2 数据包序列组装（对应 EspProvisioningParams.generate）。"""

    def __init__(self):
        self.packets: list[bytes] = []

    def _sync(self) -> bytes:
        return bytes(1048)

    def _seq_size(self, size: int) -> bytes:
        return bytes(1072 + size - 1)

    def _seq(self, seq: int) -> bytes:
        return bytes(128 + seq)

    def _data(self, data: int, idx: int) -> bytes:
        return bytes((idx << 7) | (1 << 6) | data)

    def add_block(self, buf: bytes, sequence: int, seq_crc: int, tail_is_crc: bool) -> None:
        """一组 6 字节 → 8 个 data 包（bit 切片），前面带 sync/sequence 包。"""
        if sequence == _V2_SEQUENCE_FIRST:
            self.packets.append(self._sync())
            self.packets.append(b"")          # seqSize 占位，稍后 set_total_size
            self.packets.append(self._sync())
            self.packets.append(b"")
        else:
            for _ in range(3):
                self.packets.append(self._seq(sequence))

        bit_count = 7 if tail_is_crc else 8
        for i in range(bit_count):
            data = ((buf[5] >> i) & 1) | (((buf[4] >> i) & 1) << 1) | \
                   (((buf[3] >> i) & 1) << 2) | (((buf[2] >> i) & 1) << 3) | \
                   (((buf[1] >> i) & 1) << 4) | (((buf[0] >> i) & 1) << 5)
            self.packets.append(self._data(data, i))
        if tail_is_crc:
            self.packets.append(self._data(seq_crc, 7))

    def set_total_size(self, total_blocks: int) -> None:
        """更新占位的 seqSize 包（第 1、3 个元素）。"""
        pkt = self._seq_size(total_blocks)
        self.packets[1] = pkt
        self.packets[3] = pkt

    def build(self) -> list[bytes]:
        return self.packets


def _v2_build_packets(ssid: bytes, password: bytes, aes_key: bytes | None,
                      ip: bytes) -> list[bytes]:
    """组装 v2 数据包序列（对应 EspProvisioningParams.generate）。"""
    import random
    rng = random.Random()
    crc = _V2CRC()

    # 是否加密：仅当提供 aes_key 且 password/reserved 非空
    will_encrypt = aes_key is not None and len(password) > 0
    security_ver = 2 if will_encrypt else 0
    is_ipv4 = len(ip) == 4

    # ssidEncode：非 ASCII 才需 encode（Android checkCharEncode）
    def need_encode(data: bytes) -> bool:
        return any(b < 0 or b > 127 for b in data)

    password_encode = False
    reserved_encode = False
    ssid_encode = need_encode(ssid)

    ssid_info = len(ssid) | (0b10000000 if ssid_encode else 0)
    pwd_info = len(password) | (0b10000000 if password_encode else 0)
    reserved_info = 0  # 无 reserved data

    crc.reset()
    crc.update(b"")  # bssid 为空
    bssid_crc = crc.get()

    flag = (1 if is_ipv4 else 0) | (security_ver << 1) | (0 << 3) | (0 << 6)
    head = bytes([ssid_info, pwd_info, reserved_info, bssid_crc, flag, 0])
    crc.reset()
    crc.update(head[:5])
    head = head[:5] + bytes([crc.get()])

    # 加密 password（AES-128-CBC，key 共享，随机 20B IV）
    if will_encrypt:
        aes_iv = os.urandom(20)
        encrypted = _v2_encrypt_aes_cbc(aes_key, aes_iv, password)
        password = encrypted
        password_encode = True
        pwd_padding_factor = 5
        pwd_pad_len = pwd_padding_factor - len(password) % pwd_padding_factor
        if pwd_pad_len == pwd_padding_factor:
            pwd_pad_len = 0
        password_padding = _v2_rand_bytes(rng, pwd_pad_len)
    else:
        aes_iv = b""
        password_padding = b""
        pwd_padding_factor = 6

    reserved = b""
    reserved_padding = b""

    ssid_padding_factor = 5 if ssid_encode else 6
    ssid_pad_len = ssid_padding_factor - len(ssid) % ssid_padding_factor
    if ssid_pad_len == ssid_padding_factor:
        ssid_pad_len = 0
    ssid_padding = _v2_rand_bytes(rng, ssid_pad_len)

    # 数据流拼接
    stream = head + password + password_padding + reserved + reserved_padding + \
             aes_iv + ssid + ssid_padding

    reserved_begin = len(head) + len(password) + len(password_padding)
    iv_begin = reserved_begin + len(reserved) + len(reserved_padding)
    ssid_begin = iv_begin + len(aes_iv)

    builder = _V2PacketBuilder()
    offset = 0
    sequence = _V2_SEQUENCE_FIRST
    count = 0
    pos = 0
    while pos < len(stream):
        if sequence < _V2_SEQUENCE_FIRST + 1:
            crc_in_packet = True
            expect_len = 6
        elif offset < reserved_begin:
            crc_in_packet = password_encode
            expect_len = pwd_padding_factor
        elif offset < iv_begin:
            crc_in_packet = reserved_encode
            expect_len = 5
        elif offset < ssid_begin:
            crc_in_packet = True
            expect_len = 5
        else:
            crc_in_packet = ssid_encode
            expect_len = ssid_padding_factor

        buf = bytearray(6)
        read = min(expect_len, len(stream) - pos)
        buf[:read] = stream[pos:pos + read]
        pos += read
        offset += read

        crc.reset()
        crc.update(bytes(buf[:read]))
        seq_crc = crc.get()
        if expect_len < len(buf):
            buf[len(buf) - 1] = seq_crc
        builder.add_block(bytes(buf), sequence, seq_crc, not crc_in_packet)
        sequence += 1
        count += 1

    builder.set_total_size(count)
    return builder.build()


def send_smartconfig_v2(ssid: str, password: str, aes_key: bytes,
                        ip: str | None = None, duration_s: float = 30.0) -> dict:
    """ESPTouch v2 配网广播（AES 加密）。

    :param aes_key: 16 字节共享密钥，须与设备端 smartconfig_start_config.esp_touch_v2_key 一致
    """
    if len(aes_key) != 16:
        raise ValueError("aes_key must be 16 bytes")
    if ip is None:
        ip = _local_ip()
    ip_bytes = bytes(int(x) for x in ip.split(".")) if ip != "255.255.255.255" else bytes(4)

    packets = _v2_build_packets(ssid.encode("utf-8"), password.encode("utf-8"),
                                aes_key, ip_bytes)
    if not packets:
        raise ValueError("empty packets")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    dest = ("255.255.255.255", _V2_DEVICE_PORT)
    sent = 0
    end = time.monotonic() + duration_s
    try:
        while time.monotonic() < end:
            for pkt in packets:
                if not pkt:
                    continue
                sock.sendto(pkt, dest)
                sent += 1
                time.sleep(_V2_INTERVAL_S)
                if time.monotonic() >= end:
                    break
    finally:
        sock.close()

    logger.info("smartconfig v2 sent: ssid=%s packets=%d (%.0fs)", ssid, sent, duration_s)
    return {"sent": True, "packets": sent, "ssid": ssid, "v2": True}


# 兼容命名：v1 发送器（广播模式）
def _local_ip_legacy():
    return _local_ip()


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3:
        r = send_smartconfig(sys.argv[1], sys.argv[2])
        print(r)
    else:
        print("Usage: python esptouch.py <ssid> <password>")
