"""入口：python app.py（或 python -m qqlife，需 src 在 PYTHONPATH 或已安装）。
仅做一件事：把 src/ 加进 sys.path 后启动 qqlife.app.main。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from qqlife.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())