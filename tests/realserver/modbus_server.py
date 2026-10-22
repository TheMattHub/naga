"""A Modbus TCP server of pymodbus's (not ours), for the realserver tests: binds 127.0.0.1 only.

    python modbus_server.py PORT

Prints "READY" when it serves. Unit 1 holds: holding 0 = 215, holding 10..11 = float32 1.5
(high word first), holding 20..21 = uint32 65538, input 1 = 0xFFFE (int16 -2), coil 3 = on,
discrete input 4 = off. Needs the optional extra `nagasim[modbus]` (pymodbus 3.11+ simulator API).
"""
import asyncio
import sys

from pymodbus.server import StartAsyncTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice


def _registers(address, values):
    return SimData(address=address, values=values, datatype=DataType.REGISTERS)


def _bits(address, values):
    return SimData(address=address, values=values, datatype=DataType.BITS)


async def main(port: int) -> None:
    device = SimDevice(id=1, simdata=(
        [_bits(3, [True])],                                                  # coils
        [_bits(4, [False])],                                                 # discrete inputs
        [_registers(0, [215]), _registers(10, [0x3FC0, 0]), _registers(20, [1, 2])],   # holding
        [_registers(1, [0xFFFE])]))                                          # input
    sys.stdout.write("READY\n")
    sys.stdout.flush()
    await StartAsyncTcpServer(context=device, address=("127.0.0.1", port))


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
