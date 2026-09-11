"""
Reliable Packet Serial Protocol - Python Implementation
Ported from packet_serial.c / packet_serial.h

Features:
- Framed binary packets with SYNC bytes, sequence IDs, chunking, and CRC-16-CCITT validation.
- Selective ACK/NACK framing with retry mechanism.
- Cross-platform PySerial binding for Windows, Linux, and macOS.
- Built-in interactive CLI monitor.
"""

import sys
import time
import threading
from enum import IntEnum
from typing import Callable, Optional, Union, Sequence, List
import serial
import serial.tools.list_ports

try:
    import colorama
    colorama.init(autoreset=True)
except ImportError:
    pass


# ============================================================================
# Protocol Constants
# ============================================================================
SYNC1 = 0xAA
SYNC2 = 0xBB
END_BYTE = 0xAB

FLAG_LAST_CHUNK = 0x01
FLAG_ACK = 0x02
FLAG_NACK = 0x04

MAX_PAYLOAD_SIZE = 64
HEADER_SIZE = 6


# ============================================================================
# Enums
# ============================================================================
class PacketStatus(IntEnum):
    PACKET_OK = 0
    PACKET_ERROR = 1
    PACKET_TIMEOUT = 2
    PACKET_WRONG_CRC = 3
    PACKET_WRONG_HEADER = 4
    PACKET_WRONG_END = 5
    PACKET_WRONG_LENGTH = 6


class PacketRxState(IntEnum):
    RX_STATE_WAIT_SYNC1 = 0
    RX_STATE_WAIT_SYNC2 = 1
    RX_STATE_READ_HEADER = 2
    RX_STATE_READ_PAYLOAD = 3
    RX_STATE_READ_CRC1 = 4
    RX_STATE_READ_CRC2 = 5
    RX_STATE_WAIT_END = 6


class Colors:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def clr_print(clr: str = Colors.RESET, val: str = "") -> None:
    """Print formatted string with ANSI color code reset."""
    print(f"{clr}{val}{Colors.RESET}")


def get_available_serial_ports() -> List[serial.tools.list_ports_common.ListPortInfo]:
    """
    Find available serial ports across operating systems (Windows, Linux, macOS).
    Prioritizes ports matching common USB serial identifiers.
    """
    all_ports = list(serial.tools.list_ports.comports())
    usb_ports = []

    for port_info in all_ports:
        device = port_info.device or ""
        desc = port_info.description or ""
        hwid = port_info.hwid or ""
        mfg = getattr(port_info, "manufacturer", "") or ""
        prod = getattr(port_info, "product", "") or ""

        combined = f"{device} {desc} {hwid} {mfg} {prod}".upper()
        if any(keyword in combined for keyword in ["USB", "COM", "ACM", "FTDI", "CH340", "CP210", "SILICON", "STLINK", "SERIAL"]):
            usb_ports.append(port_info)

    return usb_ports if usb_ports else all_ports


# ============================================================================
# CRC Calculation (CRC-16-CCITT 0x1021, Init 0xFFFF)
# ============================================================================
def calculate_crc16(data: Union[bytes, bytearray, Sequence[int]]) -> int:
    """
    Calculates CRC-16-CCITT (Poly: 0x1021, Init: 0xFFFF).
    Matches STM32 HAL_CRC_Calculate with 16-bit poly 0x1021, init 0xFFFF.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


# ============================================================================
# ReliablePacketer Class
# ============================================================================
class ReliablePacketer:
    def __init__(self, ack_timeout_ms: int = 500, serial_obj: Optional[serial.Serial] = None):
        self.tx_func: Optional[Callable[[bytes], None]] = None
        self.available_func: Optional[Callable[[], bool]] = None
        self.read_func: Optional[Callable[[], Optional[int]]] = None
        self.millis_func: Callable[[], int] = lambda: int(time.time() * 1000)

        self.ack_timeout_ms = ack_timeout_ms
        self.callback: Optional[Callable[[bytes], None]] = None

        self.rx_buffer = bytearray()
        self.rx_buffer_size = 4096
        self.rx_data_len = 0

        self.current_msg_id = 0
        self.expected_chunk_id = 0
        self.tx_msg_id = 0

        self.rx_state = PacketRxState.RX_STATE_WAIT_SYNC1
        self.rx_header = bytearray(HEADER_SIZE)
        self.rx_header_index = 0

        self.rx_chunk_payload = bytearray(MAX_PAYLOAD_SIZE)
        self.rx_payload_index = 0

        self.rx_crc_bytes = bytearray(2)

        if serial_obj is not None:
            self.bind_serial(serial_obj)

    def bind_serial(self, ser: serial.Serial) -> None:
        """Binds a PySerial serial instance for data transmission and reception."""
        def _write(data: bytes):
            try:
                if ser and ser.is_open:
                    ser.write(data)
            except Exception:
                pass

        def _available() -> bool:
            try:
                return bool(ser and ser.is_open and ser.in_waiting > 0)
            except Exception:
                return False

        def _read() -> Optional[int]:
            try:
                if not (ser and ser.is_open):
                    return None
                b = ser.read(1)
                return b[0] if b else None
            except Exception:
                return None

        self.tx_func = _write
        self.available_func = _available
        self.read_func = _read
        self.millis_func = lambda: int(time.time() * 1000)

    def set_callbacks(
        self,
        tx_func: Callable[[bytes], None],
        available_func: Callable[[], bool],
        read_func: Callable[[], Optional[int]],
        millis_func: Optional[Callable[[], int]] = None,
    ) -> None:
        """Configures custom I/O callbacks for transport independence."""
        self.tx_func = tx_func
        self.available_func = available_func
        self.read_func = read_func
        if millis_func:
            self.millis_func = millis_func

    def on_receive(self, callback: Callable[[bytes], None]) -> None:
        """Registers a callback function to handle complete assembled packets."""
        self.callback = callback

    def set_receive_buffer(self, buffer_size: int) -> None:
        """Configures the internal RX assembly buffer size limit."""
        self.rx_buffer_size = buffer_size

    def reset_rx_state(self) -> None:
        """Resets the packet receiver finite state machine."""
        self.rx_state = PacketRxState.RX_STATE_WAIT_SYNC1
        self.rx_header_index = 0
        self.rx_payload_index = 0

    def _send_ack(self, msg_id: int, chunk_id: int, is_ack: bool) -> None:
        """Sends an ACK or NACK response packet back to the transmitter."""
        if not self.tx_func:
            return

        ack_frame = bytearray(9)
        ack_frame[0] = SYNC1
        ack_frame[1] = SYNC2
        ack_frame[2] = msg_id & 0xFF
        ack_frame[3] = chunk_id & 0xFF
        ack_frame[4] = FLAG_ACK if is_ack else FLAG_NACK
        ack_frame[5] = 0

        crc = calculate_crc16(ack_frame[:HEADER_SIZE])
        ack_frame[6] = (crc >> 8) & 0xFF
        ack_frame[7] = crc & 0xFF
        ack_frame[8] = END_BYTE

        self.tx_func(bytes(ack_frame))

    def _process_received_chunk(self) -> None:
        """Validates received chunk header, CRC, and reassembles payload."""
        msg_id = self.rx_header[2]
        chunk_id = self.rx_header[3]
        flags = self.rx_header[4]
        length = self.rx_header[5]

        if length > 0:
            temp_buf = bytes(self.rx_header) + bytes(self.rx_chunk_payload[:length])
        else:
            temp_buf = bytes(self.rx_header)

        expected_crc = calculate_crc16(temp_buf)
        received_crc = (self.rx_crc_bytes[0] << 8) | self.rx_crc_bytes[1]

        if expected_crc != received_crc:
            self._send_ack(msg_id, chunk_id, False)
            return

        self._send_ack(msg_id, chunk_id, True)

        if msg_id != self.current_msg_id or chunk_id == 0:
            self.current_msg_id = msg_id
            self.expected_chunk_id = 0
            self.rx_data_len = 0
            self.rx_buffer = bytearray()

        if chunk_id == self.expected_chunk_id:
            if length > 0:
                self.rx_buffer.extend(self.rx_chunk_payload[:length])
                self.rx_data_len += length
            self.expected_chunk_id = (self.expected_chunk_id + 1) & 0xFF

            if flags & FLAG_LAST_CHUNK:
                full_payload = bytes(self.rx_buffer[: self.rx_data_len])
                if self.callback:
                    self.callback(full_payload)
                self.expected_chunk_id = 0

    def send(self, data: Union[bytes, bytearray]) -> PacketStatus:
        """
        Splits data into framed chunks, sends over transport, and waits for ACKs.
        Retries up to 3 times per chunk before timing out.
        """
        if not data or not self.tx_func or not self.millis_func or not self.available_func or not self.read_func:
            return PacketStatus.PACKET_ERROR

        self.tx_msg_id = (self.tx_msg_id + 1) & 0xFF
        msg_id = self.tx_msg_id

        data_len = len(data)
        total_chunks = (data_len + MAX_PAYLOAD_SIZE - 1) // MAX_PAYLOAD_SIZE

        for chunk_idx in range(total_chunks):
            offset = chunk_idx * MAX_PAYLOAD_SIZE
            chunk_payload = data[offset : offset + MAX_PAYLOAD_SIZE]
            chunk_len = len(chunk_payload)

            is_last = chunk_idx == total_chunks - 1

            frame = bytearray()
            frame.append(SYNC1)
            frame.append(SYNC2)
            frame.append(msg_id)
            frame.append(chunk_idx & 0xFF)
            frame.append(FLAG_LAST_CHUNK if is_last else 0x00)
            frame.append(chunk_len)
            frame.extend(chunk_payload)

            crc_calc_len = HEADER_SIZE + chunk_len
            crc = calculate_crc16(frame[:crc_calc_len])

            frame.append((crc >> 8) & 0xFF)
            frame.append(crc & 0xFF)
            frame.append(END_BYTE)

            retry = 3
            ack_received = False

            while retry > 0 and not ack_received:
                retry -= 1
                self.tx_func(bytes(frame))

                start_time = self.millis_func()
                waiting = True

                ack_state = 0
                ack_msg = 0
                ack_chunk = 0
                ack_flags = 0

                while (self.millis_func() - start_time) < self.ack_timeout_ms and waiting:
                    while self.available_func() and waiting:
                        b = self.read_func()
                        if b is None:
                            break
                        if isinstance(b, (bytes, bytearray)):
                            if len(b) == 0:
                                break
                            b = b[0]

                        if ack_state == 0:
                            if b == SYNC1:
                                ack_state = 1
                        elif ack_state == 1:
                            if b == SYNC2:
                                ack_state = 2
                            else:
                                ack_state = 1 if b == SYNC1 else 0
                        elif ack_state == 2:
                            ack_msg = b
                            ack_state = 3
                        elif ack_state == 3:
                            ack_chunk = b
                            ack_state = 4
                        elif ack_state == 4:
                            ack_flags = b
                            ack_state = 5
                        elif ack_state == 5:
                            ack_state = 6 if b == 0 else 0
                        elif ack_state == 6:
                            ack_state = 7
                        elif ack_state == 7:
                            ack_state = 8
                        elif ack_state == 8:
                            if b == END_BYTE:
                                if ack_msg == msg_id and ack_chunk == (chunk_idx & 0xFF):
                                    if ack_flags & FLAG_ACK:
                                        ack_received = True
                                        waiting = False
                                    elif ack_flags & FLAG_NACK:
                                        waiting = False
                            ack_state = 0

                    if waiting:
                        time.sleep(0.001)

            if not ack_received:
                return PacketStatus.PACKET_TIMEOUT

        return PacketStatus.PACKET_OK

    def update(self) -> None:
        """Polls transport for incoming stream bytes and advances the receiver state machine."""
        if not self.available_func or not self.read_func:
            return

        try:
            while self.available_func():
                b = self.read_func()
                if b is None:
                    break
                if isinstance(b, (bytes, bytearray)):
                    if len(b) == 0:
                        break
                    b = b[0]

                if self.rx_state == PacketRxState.RX_STATE_WAIT_SYNC1:
                    if b == SYNC1:
                        self.rx_header[0] = b
                        self.rx_header_index = 1
                        self.rx_state = PacketRxState.RX_STATE_WAIT_SYNC2

                elif self.rx_state == PacketRxState.RX_STATE_WAIT_SYNC2:
                    if b == SYNC2:
                        self.rx_header[1] = b
                        self.rx_header_index = 2
                        self.rx_state = PacketRxState.RX_STATE_READ_HEADER
                    elif b == SYNC1:
                        self.rx_header_index = 1
                    else:
                        self.rx_state = PacketRxState.RX_STATE_WAIT_SYNC1

                elif self.rx_state == PacketRxState.RX_STATE_READ_HEADER:
                    self.rx_header[self.rx_header_index] = b
                    self.rx_header_index += 1
                    if self.rx_header_index == HEADER_SIZE:
                        if self.rx_header[4] & (FLAG_ACK | FLAG_NACK):
                            self.rx_state = PacketRxState.RX_STATE_WAIT_SYNC1
                            continue
                        if self.rx_header[5] > 0:
                            self.rx_payload_index = 0
                            self.rx_state = PacketRxState.RX_STATE_READ_PAYLOAD
                        else:
                            self.rx_state = PacketRxState.RX_STATE_READ_CRC1

                elif self.rx_state == PacketRxState.RX_STATE_READ_PAYLOAD:
                    self.rx_chunk_payload[self.rx_payload_index] = b
                    self.rx_payload_index += 1
                    if self.rx_payload_index == self.rx_header[5]:
                        self.rx_state = PacketRxState.RX_STATE_READ_CRC1

                elif self.rx_state == PacketRxState.RX_STATE_READ_CRC1:
                    self.rx_crc_bytes[0] = b
                    self.rx_state = PacketRxState.RX_STATE_READ_CRC2

                elif self.rx_state == PacketRxState.RX_STATE_READ_CRC2:
                    self.rx_crc_bytes[1] = b
                    self.rx_state = PacketRxState.RX_STATE_WAIT_END

                elif self.rx_state == PacketRxState.RX_STATE_WAIT_END:
                    if b == END_BYTE:
                        self._process_received_chunk()
                    self.reset_rx_state()
        except Exception:
            pass


# ============================================================================
# C-Style Function API Wrappers
# ============================================================================
def packeter_init(ctx: ReliablePacketer, ack_timeout_ms: int = 500) -> None:
    ctx.ack_timeout_ms = ack_timeout_ms
    ctx.reset_rx_state()


def packeter_set_callbacks(ctx: ReliablePacketer, tx_func, available_func, read_func, millis_func=None) -> None:
    ctx.set_callbacks(tx_func, available_func, read_func, millis_func)


def packeter_on_receive(ctx: ReliablePacketer, callback) -> None:
    ctx.on_receive(callback)


def packeter_set_receive_buffer(ctx: ReliablePacketer, buffer_size: int) -> None:
    ctx.set_receive_buffer(buffer_size)


def packeter_send(ctx: ReliablePacketer, data: Union[bytes, bytearray]) -> PacketStatus:
    return ctx.send(data)


def packeter_update(ctx: ReliablePacketer) -> None:
    ctx.update()


# ============================================================================
# Serial Monitor CLI
# ============================================================================
def serial_monitor() -> None:
    clr_print(Colors.CYAN, "==================================================")
    clr_print(Colors.CYAN, "       STM32 Reliable Packet Serial Monitor       ")
    clr_print(Colors.CYAN, "==================================================\n")

    ports = get_available_serial_ports()
    if ports:
        clr_print(Colors.YELLOW, "Available Serial Ports:")
        for idx, p in enumerate(ports):
            clr_print(Colors.GREEN, f"  {idx + 1}) {p.device} - {p.description}")
        print()

    default_port = ports[0].device if ports else ("COM1" if sys.platform == "win32" else "/dev/ttyACM0")

    port_input = input(f"Enter port (e.g. {default_port} or index) [{default_port}]: ").strip()
    if not port_input:
        port = default_port
    elif port_input.isdigit() and 1 <= int(port_input) <= len(ports):
        port = ports[int(port_input) - 1].device
    else:
        port = port_input

    baud_input = input("Enter baudrate [115200]: ").strip()
    baudrate = int(baud_input) if baud_input.isdigit() else 115200

    try:
        ser = serial.Serial(port, baudrate, timeout=0.01)
        clr_print(Colors.GREEN, f"\nSuccessfully connected to {port} @ {baudrate} baud.")
    except Exception as e:
        clr_print(Colors.RED, f"Error: Failed to open port '{port}': {e}")
        sys.exit(1)

    packeter = ReliablePacketer(ack_timeout_ms=500, serial_obj=ser)

    def on_packet_received(data: bytes):
        timestamp = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
        clr_print(Colors.GREEN, f"\n[{timestamp}] [RX PACKET] Received {len(data)} bytes:")

        hex_str = " ".join(f"{b:02X}" for b in data)
        clr_print(Colors.CYAN, f"  HEX : {hex_str}")

        try:
            text = data.decode("utf-8")
            clr_print(Colors.YELLOW, f"  TEXT: {text}")
        except UnicodeDecodeError:
            printable = "".join(chr(b) if 32 <= b <= 126 else "." for b in data)
            clr_print(Colors.YELLOW, f"  RAW : {printable}")
        print()

    packeter.on_receive(on_packet_received)

    running = True

    def input_thread_func():
        nonlocal running
        clr_print(Colors.YELLOW, "Commands:")
        clr_print(Colors.YELLOW, "  - Type text and press Enter to send a text packet")
        clr_print(Colors.YELLOW, "  - Type /hex <bytes> (e.g. /hex AA BB 01 02) to send raw hex")
        clr_print(Colors.YELLOW, "  - Type /quit or /exit to stop monitor\n")

        while running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                text = line.strip()
                if not text:
                    continue
                if text.lower() in ["/quit", "/exit"]:
                    running = False
                    break
                elif text.lower().startswith("/hex "):
                    hex_part = text[5:].strip().replace(" ", "")
                    try:
                        data = bytes.fromhex(hex_part)
                        clr_print(Colors.CYAN, f"Sending {len(data)} bytes (HEX)...")
                        res = packeter.send(data)
                        if res == PacketStatus.PACKET_OK:
                            clr_print(Colors.GREEN, "Packet sent & ACKed successfully!")
                        else:
                            clr_print(Colors.RED, f"Packet send failed: {res.name}")
                    except ValueError as ve:
                        clr_print(Colors.RED, f"Invalid hex string: {ve}")
                else:
                    data = text.encode("utf-8")
                    clr_print(Colors.CYAN, f"Sending {len(data)} bytes: '{text}'...")
                    res = packeter.send(data)
                    if res == PacketStatus.PACKET_OK:
                        clr_print(Colors.GREEN, "Packet sent & ACKed successfully!")
                    else:
                        clr_print(Colors.RED, f"Packet send failed: {res.name}")
            except Exception:
                break

    t = threading.Thread(target=input_thread_func, daemon=True)
    t.start()

    try:
        while running:
            packeter.update()
            time.sleep(0.001)
    except KeyboardInterrupt:
        print("\nExiting monitor...")
    finally:
        running = False
        ser.close()
        clr_print(Colors.YELLOW, "Serial port closed.")


def main() -> None:
    serial_monitor()


if __name__ == "__main__":
    main()
