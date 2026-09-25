"""接口测试环境：每个测试会话使用独立的临时 SQLite 文件。"""

import os
import tempfile

import pytest

# 必须在导入应用代码之前指定数据库，app.config 在导入时读取环境变量
_TMP_DIR = tempfile.mkdtemp(prefix="robot-audit-test-")
_DB_PATH = os.path.join(_TMP_DIR, "test_robot_data.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.setdefault("API_V1_PREFIX", "/api/v1")


@pytest.fixture(scope="session")
def db_path():
    return _DB_PATH
