# -*- coding: utf-8 -*-
"""支持 python -m screenqa 启动。"""

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
