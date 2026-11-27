"""Read-only Modbus TCP access built on PyModbus (3.11.3, as the existing monitor uses).

The wrapper exposes exactly two operations, both reads: read input registers (function code 4) and, for the vendor's documented read-only look at
device settings, read holding registers (function code 3). The PyModbus client is private and never handed out, so no caller can reach a write
method. Addresses are used verbatim (no +/-1 adjustment), as in the vendor's client.
"""
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException
from pymodbus.pdu import ExceptionResponse
from pymodbus.pdu.register_message import ReadHoldingRegistersResponse, ReadInputRegistersResponse

from .errors import DeviceError

MAX_REGISTERS = 125          # Modbus limit for one read


class ReadOnlyModbusTcp:
    def __init__(self, host, port=502, unit=1, timeout=2.0, client_factory=ModbusTcpClient):
        if not host or not 1 <= port <= 65535 or not 0 <= unit <= 255 or not 0 < timeout <= 30:
            raise ValueError("Invalid Modbus endpoint settings")
        self.__host, self.__port, self.__unit, self.__timeout = host, port, unit, timeout
        self.__factory = client_factory
        self.__client = None

    def set_host(self, host):
        """Point at another address (the device moved). Drops any open connection; the next read connects to the new one."""
        if not isinstance(host, str) or not host or host != host.strip():
            raise ValueError("Invalid host")
        if host != self.__host:
            self.close()
            self.__host = host

    def read_input_registers(self, address, count):
        return self.__read(4, address, count)

    def read_holding_registers(self, address, count):
        """A read of the device's settings registers (function code 3). Reading never changes the device."""
        return self.__read(3, address, count)

    def __read(self, function, address, count):
        if type(address) is not int or type(count) is not int:
            raise ValueError("Address and count must be integers")
        if not 0 <= address <= 0xFFFF or not 1 <= count <= MAX_REGISTERS or address + count > 0x10000:
            raise ValueError("Register range outside Modbus limits")
        started = time.monotonic()
        try:
            client = self.__connected()
            result = (client.read_input_registers if function == 4 else client.read_holding_registers)(address, count=count, device_id=self.__unit)
        except DeviceError:
            raise
        except TimeoutError:
            self.close()
            raise DeviceError("timeout", "Device did not answer in time") from None
        except ModbusIOException:
            self.close()
            raise DeviceError("timeout", "No valid reply from the device") from None
        except (ConnectionException, ConnectionError, OSError):
            self.close()
            raise DeviceError("closed", "Connection to the device failed") from None
        except ModbusException:
            self.close()
            raise DeviceError("protocol", "Invalid Modbus reply") from None
        if isinstance(result, ExceptionResponse):
            # The device answered but refused: the link itself is healthy.
            raise DeviceError("exception", f"Device refused the read (Modbus exception {result.exception_code})")
        # The reply must really be a register reply of the function asked for (4 or 3), not merely something with registers.
        expected = ReadInputRegistersResponse if function == 4 else ReadHoldingRegistersResponse
        if not isinstance(result, expected) or result.function_code != function or result.isError():
            self.close()
            raise DeviceError("protocol", "Invalid Modbus reply")
        if time.monotonic() - started > self.__timeout:
            # PyModbus restarts its timer on every byte, so a drip-fed reply can overrun.
            # A reply that arrives after the deadline is old data: reject it.
            self.close()
            raise DeviceError("timeout", "Reply arrived after the deadline")
        registers = list(result.registers)
        if len(registers) != count or any(type(r) is not int or not 0 <= r <= 0xFFFF for r in registers):
            self.close()
            raise DeviceError("protocol", f"Short or invalid reply at {address}")
        return registers

    def close(self):
        client, self.__client = self.__client, None
        if client is not None:
            try:
                client.close()
            except Exception:       # closing a broken link must never raise into the caller
                pass

    def __connected(self):
        if self.__client is None:
            # retries=0: one attempt per poll; reconnect_delay=0: no hidden background reconnection.
            client = self.__factory(self.__host, port=self.__port, timeout=self.__timeout,
                                    retries=0, reconnect_delay=0)
            if not client.connect():
                client.close()
                raise DeviceError("connect", "Cannot connect to the device")
            self.__client = client
        return self.__client
