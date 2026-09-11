"""
Serial Test & Monitor for STM32 Reliable Packeter Protocol
"""

import sys
from packet import serial_monitor, ReliablePacketer, PacketStatus, calculate_crc16

if __name__ == "__main__":
    serial_monitor()
