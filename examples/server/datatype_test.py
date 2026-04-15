# pyright: reportUnusedCallResult=false
"""Datatype Test — exercises all supported BlaeckTCP datatypes.

Useful for verifying protocol encoding — connect Loggbok and check
that all values display correctly at their expected min/max/special values.
"""

import time

from blaecktcpy import BlaeckTCPy

ip = "127.0.0.1"
port = 23

bltcp = BlaeckTCPy(
            ip=ip,
            port=port,
            device_name="Datatype Test",
        )

bltcp.add_signal("Bool_false", "bool", False)
bltcp.add_signal("Bool_true", "bool", True)
bltcp.add_signal("Byte_min", "byte", 0)
bltcp.add_signal("Byte_max", "byte", 255)
bltcp.add_signal("Short_min", "short", -32768)
bltcp.add_signal("Short_max", "short", 32767)
bltcp.add_signal("UShort_min", "unsigned short", 0)
bltcp.add_signal("UShort_max", "unsigned short", 65535)
bltcp.add_signal("Int_min", "int", -2147483648)
bltcp.add_signal("Int_max", "int", 2147483647)
bltcp.add_signal("UInt_min", "unsigned int", 0)
bltcp.add_signal("UInt_max", "unsigned int", 4294967295)
bltcp.add_signal("Long_min", "long", -2147483648)
bltcp.add_signal("Long_max", "long", 2147483647)
bltcp.add_signal("ULong_min", "unsigned long", 0)
bltcp.add_signal("ULong_max", "unsigned long", 4294967295)
bltcp.add_signal("Float_min", "float", -3.4028235e38)
bltcp.add_signal("Float_max", "float", 3.4028235e38)
bltcp.add_signal("Float_NaN", "float", float("nan"))
bltcp.add_signal("Float_Inf", "float", float("inf"))
bltcp.add_signal("Float_NegInf", "float", float("-inf"))
bltcp.add_signal("Double_min", "double", -1.7976931348623157e308)
bltcp.add_signal("Double_max", "double", 1.7976931348623157e308)
bltcp.add_signal("Double_NaN", "double", float("nan"))
bltcp.add_signal("Double_Inf", "double", float("inf"))
bltcp.add_signal("Double_NegInf", "double", float("-inf"))

bltcp.start()
print("##LOGGBOK:READY##")  # Sentinel for Loggbok's process launcher — safe to remove
while True:
    bltcp.tick()
    time.sleep(0.001)  # Prevent busy loop; reduce or remove if faster response is needed
