import binascii
import socket
import struct
import time

LIB_VERSION = "3.0.0"  # BlaeckTCP protocol version implemented
LIB_NAME = "BlaeckTCP"


class Signal:
    def __init__(self, signal_name, datatype, value=0):
        self.signal_name = signal_name
        self.datatype = datatype
        self.datatypes = {
            'bool': 0,
            'byte': 1,
            'short': 2,
            'unsigned short': 3,
            'int': 6,           # 32-bit platform: maps to long (DTYPE 6, 4 bytes)
            'unsigned int': 7,  # 32-bit platform: maps to unsigned long (DTYPE 7, 4 bytes)
            'long': 6,
            'unsigned long': 7,
            'float': 8,
            'double': 9
        }
        self.datatype_bytes = {
            'bool': 1,
            'byte': 1,
            'short': 2,
            'unsigned short': 2,
            'int': 4,
            'unsigned int': 4,
            'long': 4,
            'unsigned long': 4,
            'float': 4,
            'double': 8
        }
        if not isinstance(value, int) and self.datatype not in ['float', 'double']:
            value = int(value)
        self._value = value

    @property
    def value(self):
        if self.datatype in ['float', 'double']:
            return self.floaters()
        else:
            return self.integers()

    @value.setter
    def value(self, value):
        if not isinstance(value, int) and self.datatype not in ['float', 'double']:
            value = int(value)
        self._value = value

    def integers(self, reihenfolge='little'):
        signed = self.datatype in ('short', 'int', 'long')
        return self._value.to_bytes(self.datatype_bytes[self.datatype], reihenfolge, signed=signed)

    def floaters(self):
        datatype_format = {'float': 'f', 'double': 'd'}
        return struct.pack(datatype_format[self.datatype], self._value)

    def get_dtype(self):
        return self.datatypes[self.datatype].to_bytes(1, 'little')


class blaecktcpy:
    def __init__(self, device_name, device_hw_version, device_fw_version, ip, port, newline_cr='\r\n'):
        self.signals = []
        self.device_name = device_name.encode()
        self.device_hw_version = device_hw_version.encode()
        self.device_fw_version = device_fw_version.encode()
        self._timed_activated = False
        self.msg_key = {
            'Symbol List': 'B0',
            'Data': 'B1',
            'Devices': 'B3'
        }
        self.master_slave_config = bytes.fromhex('00')
        self.slave_id = bytes.fromhex('00')
        self.timer_set = 0
        self.timer = 0

        # TCP server
        self._server_socket = socket.socket()
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((ip, port))
        self._server_socket.settimeout(0.05)
        self._server_socket.listen(1)
        self._con = False
        print(f'TCP server listening on {ip}:{port}')

    def active(self):
        return self._timed_activated

    def add_signal(self, signal):
        if isinstance(signal, Signal):
            self.signals.append(signal)
        else:
            raise Exception("Variable has to be type Signal not : ", type(signal))

    def delete_signals(self):
        self.signals = []

    def connected(self):
        return bool(self._con)

    def _tcp_read(self):
        data = ''
        if not self._con:
            try:
                conn, addr = self._server_socket.accept()
                conn.setblocking(False)
                self._con = conn
                print(f'Client connected: {addr[0]}:{addr[1]}')
            except OSError:
                pass  # no client yet, try again next tick
        else:
            while True:
                try:
                    chunk = self._con.recv(1024)
                    if not chunk:
                        print('Client disconnected')
                        self._con = False
                        break
                    data += chunk.decode()
                except BlockingIOError:
                    break  # no data available right now, connection still alive
                except OSError as e:
                    print(f'Socket error: {e}')
                    self._con = False
                    break
                if '>' in data:
                    break  # complete message received
        return data

    def _tcp_send(self, data):
        if self._con:
            try:
                self._con.send(data)
                return True
            except OSError as e:
                print(f'Send error: {e}')
                self._con = False
                return False
        return False

    def _decode_four_byte(self, data):
        # Format: <BLAECK.ACTIVATE,b0,b1,b2,b3>
        # e.g. <BLAECK.ACTIVATE,96,234> = 60000 ms
        ans = data.replace('>', '').split(',')
        return_data = 0
        for i in range(1, len(ans)):
            try:
                return_data += int(ans[i]) << ((i - 1) * 8)
            except ValueError:
                pass
        return return_data

    def read(self):
        data = self._tcp_read()
        data = data.replace('\x00', '')  # strip null keep-alive bytes
        if 'BLAECK.WRITE_SYMBOLS' in data:
            self.write_symbols(self._decode_four_byte(data))
        elif 'BLAECK.WRITE_DATA' in data:
            self.write_data(self._decode_four_byte(data))
        elif 'BLAECK.GET_DEVICES' in data:
            self.write_devices(self._decode_four_byte(data))
        elif 'BLAECK.ACTIVATE' in data:
            self.timer_set = self._decode_four_byte(data)
            self._timed_activated = True
            print(f'Timed data activated: interval={self.timer_set} ms')
        elif 'BLAECK.DEACTIVATE' in data:
            self._timed_activated = False
            print('Timed data deactivated')

    def write_symbols(self, msg_id):
        if self.connected():
            bytes_bf_data = bytes.fromhex(self.msg_key['Symbol List']) + b':' + msg_id.to_bytes(4, 'little') + b':'
            data = b'<BLAECK:' + bytes_bf_data + self._get_symbols() + b'/BLAECK>\r\n'
            self._tcp_send(data)

    def write_data(self, msg_id):
        if self.connected():
            bytes_bf_data = bytes.fromhex(self.msg_key['Data']) + b':' + msg_id.to_bytes(4, 'little') + b':'
            data = b'<BLAECK:' + self._get_data_with_crc(bytes_bf_data) + b'/BLAECK>\r\n'
            self._tcp_send(data)

    def timed_write_data(self, msg_id):
        if self.connected() and (time.time_ns() - self.timer) / 1000000 > self.timer_set:
            bytes_bf_data = bytes.fromhex(self.msg_key['Data']) + b':' + msg_id.to_bytes(4, 'little') + b':'
            data = b'<BLAECK:' + self._get_data_with_crc(bytes_bf_data) + b'/BLAECK>\r\n'
            self.timer = time.time_ns()
            self._tcp_send(data)
            return True
        return False

    def write_devices(self, msg_id):
        if self.connected():
            bytes_bf_data = bytes.fromhex(self.msg_key['Devices']) + b':' + msg_id.to_bytes(4, 'little') + b':'
            data = b'<BLAECK:' + bytes_bf_data + self.master_slave_config + self.slave_id + self.device_name + b'\0' + self.device_hw_version + b'\0' + self.device_fw_version + b'\0' + LIB_VERSION.encode() + b'\0' + LIB_NAME.encode() + b'\0' + b'/BLAECK>\r\n'
            self._tcp_send(data)

    def tick(self, msg_id=185273099):
        self.read()
        ticked = False
        if self._timed_activated:
            ticked = self.timed_write_data(msg_id)
        return ticked

    def _get_data_with_crc(self, data_bf):
        crc_string = b''
        for idx, x in enumerate(self.signals):
            crc_string = crc_string + idx.to_bytes(2, 'little') + x.value
        crc_string = data_bf + crc_string
        crc = binascii.crc32(crc_string).to_bytes(4, 'little')
        return crc_string + bytes.fromhex('00') + crc

    def _get_symbols(self):
        ans = b''
        for x in self.signals:
            ans = ans + self.master_slave_config + self.slave_id + x.signal_name.encode() + b'\0' + x.datatypes[x.datatype].to_bytes(1, 'little')
        return ans
