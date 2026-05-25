"""
共用加载器：把 src/app_server.py 当模块加载进来，避免在 test 里重复样板代码。
（直接 from src.app_server import ... 会触发 Flask app 初始化，无副作用但更慢）
"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TARGET = _ROOT / "src" / "app_server.py"


def load_app_server():
    """加载 src/app_server.py 并返回模块对象。重复调用复用缓存。"""
    cached = sys.modules.get("_test_app_server")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("_test_app_server", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_test_app_server"] = mod
    spec.loader.exec_module(mod)
    return mod
