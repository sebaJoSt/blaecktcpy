import math
import time
from blaecktcpy import blaecktcpy, Signal

EXAMPLE_VERSION = "1.0"

ip = '127.0.0.1'
port = 23

bltcp = blaecktcpy(
    'Datatype Test',
    'Python Script',
    EXAMPLE_VERSION,
    ip,
    port
)

signals = [
    Signal('Bool_false',         'bool',           False),
    Signal('Bool_true',          'bool',           True),
    Signal('Byte_min',           'byte',           0),
    Signal('Byte_max',           'byte',           255),
    Signal('Short_min',          'short',          -32768),
    Signal('Short_max',          'short',          32767),
    Signal('UShort_min',         'unsigned short', 0),
    Signal('UShort_max',         'unsigned short', 65535),
    Signal('Int_min',            'int',            -2147483648),
    Signal('Int_max',            'int',            2147483647),
    Signal('UInt_min',           'unsigned int',   0),
    Signal('UInt_max',           'unsigned int',   4294967295),
    Signal('Long_min',           'long',           -2147483648),
    Signal('Long_max',           'long',           2147483647),
    Signal('ULong_min',          'unsigned long',  0),
    Signal('ULong_max',          'unsigned long',  4294967295),
    Signal('Float_min',          'float',          -3.4028235E+38),
    Signal('Float_max',          'float',          3.4028235E+38),
    Signal('Float_NaN',          'float',          float('nan')),
    Signal('Float_Inf',          'float',          float('inf')),
    Signal('Float_NegInf',       'float',          float('-inf')),
    Signal('Double_min',         'double',         -1.7976931348623157E+308),
    Signal('Double_max',         'double',         1.7976931348623157E+308),
    Signal('Double_NaN',         'double',         float('nan')),
    Signal('Double_Inf',         'double',         float('inf')),
    Signal('Double_NegInf',      'double',         float('-inf')),
]

for s in signals:
    bltcp.add_signal(s)

print("##LOGGBOK:READY##")

while True:
    bltcp.tick()
    time.sleep(0.001)
