"""Direct, read-only device readers (Solarbank, smart meter, Shelly).

Nothing here can write to equipment: the Modbus client only builds function
code 4 (read input registers) and the Shelly reader only issues HTTP GET.
"""
from .errors import DeviceError

__all__ = ["DeviceError"]
