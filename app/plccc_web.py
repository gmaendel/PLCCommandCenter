"""Entry point for the v2 dashboard (LaunchAgent com.plccommandcenter.web), served by waitress."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plccc.web import main  # noqa: E402

if __name__ == '__main__':
    main()
