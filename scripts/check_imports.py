"""Import every module the services need (no pollers started, nothing sent)."""
import os
import sys
import warnings

warnings.filterwarnings('ignore')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'app'))

import pycomm3, pymodbus, snap7, requests, flask, waitress  # noqa: E401,F401
from pymodbus.client import ModbusTcpClient  # noqa: F401
import snap7.client  # noqa: F401
import plccc_settings  # noqa: F401
from plccc import db, drivers, alarms, alerts, poller, web  # noqa: F401

print('imports OK: python %s | pycomm3 %s | pymodbus %s | snap7 %s | flask %s | waitress ok'
      % (sys.version.split()[0], pycomm3.__version__, pymodbus.__version__,
         getattr(snap7, '__version__', '?'), flask.__version__ if hasattr(flask, '__version__') else '?'))
