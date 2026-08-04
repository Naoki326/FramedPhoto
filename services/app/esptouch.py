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

logger = logging.getLogger(__name__)

_PORT = 7001
_SEND_INTERVAL_S = 0.008          # 8ms 一包
_GUIDE_DURATION_S = 2.0
_DATA_DURATION_S = 4.0


class _Buffer:
    """长度编码发送缓冲：包内容无关紧要，长度即信息。"""

    def __init__(self):
        self.buf = bytearray(600)
        self.data_to_send: list[int] = []
        self.address_count = 0

    def _next_target(self) -> tuple[str, int]:
        self.address_count += 1
        addr = f"234.{self.address_count % 100}.{self.address_count % 100}.{self.address_count % 100}"
        return (addr, _PORT)

    def _send_packet(self, sock: socket.socket, dest: tuple, size: int) -> None:
        sock.sendto(self.buf[0:size], dest)

    def _make_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
                     bssid: str = "", duration_s: float | None = None) -> dict:
    """发送 ESPTouch v1 配网广播。

    :param ssid:     目标 WiFi SSID（UTF-8）
    :param password: WiFi 密码
    :param ip:       发送方 IP（可选，默认取本机内网 IP；协议要求 4 字节）
    :param bssid:    可选 BSSID（hex 字符串，如 "aabbccddeeff"）
    :param duration_s: 发送时长（默认 6s：guide 2s + data 4s）
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

    buf = _Buffer()
    sock = buf._make_socket()
    try:
        # Guide：4 个引导包轮发 2 秒
        guide = (515, 514, 513, 512)
        index = 0
        next_time = time.monotonic()
        end = time.monotonic() + _GUIDE_DURATION_S
        while time.monotonic() < end or index != 0:
            now = time.monotonic()
            if now >= next_time:
                buf._send_packet(sock, buf._next_target(), guide[index])
                next_time = now + _SEND_INTERVAL_S
                index = (index + 1) % 4

        # Data：包长度序列轮发 4 秒
        index = 0
        next_time = time.monotonic()
        end = time.monotonic() + (duration_s or _DATA_DURATION_S)
        packets = 0
        while time.monotonic() < end or index != 0:
            now = time.monotonic()
            if now >= next_time:
                buf._send_packet(sock, buf._next_target(), data_seqs[index])
                next_time = now + _SEND_INTERVAL_S
                packets += 1
                index = (index + 1) % len(data_seqs)
    finally:
        sock.close()

    logger.info("smartconfig sent: ssid=%s packets=%d", ssid, packets)
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


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3:
        r = send_smartconfig(sys.argv[1], sys.argv[2])
        print(r)
    else:
        print("Usage: python esptouch.py <ssid> <password>")
