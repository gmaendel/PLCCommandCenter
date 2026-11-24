"""Entry point for the unified PLC poller/alerter (LaunchAgent com.plccommandcenter.poller).

Must run under the CommandLineTools Python.app binary (Full Disk Access for chat.db 'ack').
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plccc.poller import main  # noqa: E402

if __name__ == '__main__':
    main()
