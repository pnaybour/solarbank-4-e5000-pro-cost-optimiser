"""The ONLY place that can write to the Solarbank (Stage C). The live transport uses it, and only once all three root-owned gates of `live_gate` hold.

A deliberately narrow writer for exactly two holding registers, written the way the vendor's own integration writes them (`modbus_client.py` `write_register`):
    10064  operating mode, UINT16, function code 06, allowed values 0, 3 and 6 (and 7, the Anker's dynamic pricing, ONLY for a writer built with
           `extra_modes=(7,)`: the commissioning test `dynamic-mode` builds one; the controller's writer never does)
    10071  battery power set-point, INT32 in watts, function code 16 (two registers, high word first, negative as two's complement), |value| <= P_MAX_W
Every other address and every other value is refused before anything is sent. One attempt, no retries, a reply that arrives after its deadline is treated as a failure, and the reply
must echo the address (and the count for function 16). The pymodbus client is private; nothing outside this class can reach a write method of it."""
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException
from pymodbus.pdu import ExceptionResponse

from .errors import DeviceError

MODE_REGISTER, SETPOINT_REGISTER = 10064, 10071
ALLOWED_MODES = (0, 3, 6)
EXPLORATORY_MODES = (7,)            # the vendor's dynamic pricing ("Dynamic Tariff" in the Anker app): never written by the controller. A writer accepts it only when
                                    # built with extra_modes=(7,), which only the commissioning test `dynamic-mode` does (owner, 15 November 2026)
P_MAX_W = 3600                      # the device's own ceiling; the installation's limit (2,500 W) is applied by the garage regulation before this


class GuardedModbusWriter:
    def __init__(self, host, port=502, unit=1, timeout=2.0, client_factory=ModbusTcpClient, extra_modes=()):
        if not callable(host) and not host or not 1 <= port <= 65535 or not 0 <= unit <= 255 or not 0 < timeout <= 10:
            raise ValueError("Invalid Modbus endpoint settings")
        extra_modes = tuple(extra_modes)
        if any(type(m) is not int or m not in EXPLORATORY_MODES for m in extra_modes):
            raise ValueError(f"extra_modes may only hold {EXPLORATORY_MODES}")
        self.__modes = ALLOWED_MODES + extra_modes
        self.__host_source = host                 # an address, or a callable giving the device's current address (it can move)
        self.__connected_host = None
        self.__host, self.__port, self.__unit, self.__timeout = host, port, unit, timeout
        self.__factory = client_factory
        self.__client = None

    # ---- the two permitted writes ---------------------------------------------------------------------------------------------------------
    def write_mode(self, value, host=None):
        if type(value) is not int or value not in self.__modes:
            raise ValueError(f"Mode {value!r} is not one of {self.__modes}")
        self.__send(6, MODE_REGISTER, [value], host)

    def write_setpoint(self, watts, host=None):
        if type(watts) is not int or abs(watts) > P_MAX_W:
            raise ValueError(f"Set-point {watts!r} is not an integer within +/-{P_MAX_W} W")
        raw = watts + 0x100000000 if watts < 0 else watts
        self.__send(16, SETPOINT_REGISTER, [(raw >> 16) & 0xFFFF, raw & 0xFFFF], host)

    # ---- the transport --------------------------------------------------------------------------------------------------------------------
    def __send(self, function, address, values, host=None):
        started = time.monotonic()
        try:
            client = self.__connected(host)
            if function == 6:
                result = client.write_register(address, values[0], device_id=self.__unit)
            else:
                result = client.write_registers(address, values, device_id=self.__unit)
        except DeviceError:
            raise
        except TimeoutError:
            self.close()
            raise DeviceError("timeout", "Device did not answer the write in time") from None
        except ModbusIOException:
            self.close()
            raise DeviceError("timeout", "No valid reply to the write") from None
        except (ConnectionException, ConnectionError, OSError):
            self.close()
            raise DeviceError("closed", "Connection to the device failed during the write") from None
        except ModbusException:
            self.close()
            raise DeviceError("protocol", "Invalid Modbus reply to the write") from None
        if isinstance(result, ExceptionResponse) or result.isError():
            raise DeviceError("exception", f"Device refused the write (Modbus exception {getattr(result, 'exception_code', '?')})")
        if getattr(result, "address", address) != address or (function == 16 and getattr(result, "count", len(values)) != len(values)):
            self.close()
            raise DeviceError("protocol", "The reply does not match the write that was sent")
        if time.monotonic() - started > self.__timeout:
            self.close()
            raise DeviceError("timeout", "The reply to the write arrived after its deadline")

    def close(self):
        client, self.__client = self.__client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def __current_host(self):
        source = self.__host_source
        host = source() if callable(source) else source
        if not host:
            raise DeviceError("closed", "The device's address is not known")
        return host

    def __connected(self, host=None):
        host = host or self.__current_host()                # a caller that has just verified an address passes it: that exact address is used, never looked up again
        if self.__client is not None and host != self.__connected_host:
            self.close()                                    # the device moved: never keep writing to the old address
        if self.__client is None:
            client = self.__factory(host, port=self.__port, timeout=self.__timeout, retries=0, reconnect_delay=0)
            if not client.connect():
                client.close()
                raise DeviceError("closed", "Could not connect to the device")
            self.__client, self.__connected_host = client, host
        return self.__client
