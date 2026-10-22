"""支持 python -m oracle_arm。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
