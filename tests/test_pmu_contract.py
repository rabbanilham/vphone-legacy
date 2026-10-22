"""Verify the real PMU C helpers without building or running the emulator."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pmu_contract_test import host_check


class PMUContractTests(unittest.TestCase):
    def test_device_and_i2c_contract(self):
        host_check()
