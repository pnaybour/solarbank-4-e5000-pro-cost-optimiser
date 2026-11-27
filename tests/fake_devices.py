"""Local fake Modbus TCP server and Shelly HTTP server for socket-level tests (loopback only)."""
import http.server
import json
import socket
import socketserver
import struct
import threading
import time


class FakeModbus(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, registers, unit=1, mode="normal", port=0):
        super().__init__(("127.0.0.1", port), _ModbusHandler)
        self.registers, self.unit, self.mode = registers, unit, mode
        self.functions, self.connections, self.requests = [], 0, []
        self.active = set()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        """Unplug the device: stop listening and drop every open connection."""
        self.shutdown()
        self.server_close()
        for sock in list(self.active):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _ModbusHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        server.connections += 1
        sock = self.request
        server.active.add(sock)
        sock.settimeout(5)
        try:
            while True:
                head = self._read(sock, 7)
                transaction, protocol, length, unit = struct.unpack(">HHHB", head)
                pdu = self._read(sock, length - 1)
                function = pdu[0]
                server.functions.append(function)
                address, count = struct.unpack(">HH", pdu[1:5]) if len(pdu) >= 5 else (0, 0)
                server.requests.append((address, count))
                mode = server.mode
                if mode == "silent":
                    time.sleep(3)
                    return
                if mode == "reset":
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    return
                if mode == "close_after_request":
                    return
                values = [server.registers.get(address + i) for i in range(count)]
                if any(v is None for v in values):
                    reply = bytes([function | 0x80, 0x02])
                else:
                    reply = bytes([function, 2 * count]) + struct.pack(f">{count}H", *values)
                txn, unit_out = transaction, unit
                if mode == "wrong_txn":
                    txn = (transaction + 7) & 0xFFFF
                if mode == "wrong_unit":
                    unit_out = (unit + 1) & 0xFF
                if mode == "short" and len(reply) > 4:
                    reply = reply[:-2]
                    # length field still claims the shortened body, so the frame is consistent but the
                    # byte count disagrees with the register count
                if mode == "bad_protocol":
                    protocol = 1
                if mode == "wrong_function":
                    reply = bytes([0x03]) + reply[1:]
                frame = struct.pack(">HHHB", txn, protocol, len(reply) + 1, unit_out) + reply
                if mode == "partial_then_close":
                    sock.sendall(frame[:9])
                    return
                if mode == "trickle":
                    for byte in frame:
                        sock.sendall(bytes([byte]))
                        time.sleep(0.05)
                    continue
                sock.sendall(frame)
        except (OSError, ConnectionError):
            return
        finally:
            server.active.discard(sock)

    @staticmethod
    def _read(sock, size):
        data = b""
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data


class FakeShelly(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, routes, delay=0.0, port=0):
        super().__init__(("127.0.0.1", port), _ShellyHandler)
        self.routes, self.delay, self.methods = routes, delay, []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()


class _ShellyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.methods.append("GET")
        if self.server.delay:
            time.sleep(self.server.delay)
        route = self.server.routes.get(self.path)
        if route is None:
            self.send_response(404); self.end_headers(); return
        status, body, headers = route if len(route) == 3 else (*route, {})
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self.server.methods.append("POST"); self.send_response(405); self.end_headers()

    do_PUT = do_DELETE = do_POST
