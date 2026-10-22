"""Compile real board C for each blocked native I2C provider contract."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from busy_i2c_contract_test import host_check, BASEBAND, IAP, BACKLIGHT, tristar_startup_checks


class BusyI2CContracts(unittest.TestCase):
    def test_baseband_mux(self):
        host_check(BASEBAND)

    def test_accessory_connector(self):
        host_check(IAP)

    def test_backlight(self):
        host_check(BACKLIGHT)

    def test_native_tristar_startup(self):
        host_check(tristar_startup_checks())

    def test_probe139_tristar_sequence(self):
        host_check(tristar_startup_checks(observed_prefix=True))
