"""pytest 配置：统一把 db / upload / ota 隔离到临时目录。

必须在任何 `import app...` 之前执行，否则 settings 会读取默认路径，
测试可能污染开发数据（如 daily 渲染写进 services/daily）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__) + "/..")

_tmp = tempfile.mkdtemp(prefix="framedphoto-pytest-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.environ["OTA_DIR"] = os.path.join(_tmp, "ota")
