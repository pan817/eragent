"""时区改造上线脚本（跨平台）。

用途:把既有部署从 "代码混 UTC / DB 混 UTC" 切到全链路北京时间
(Asia/Shanghai)。必须按步骤执行,否则会出现写入点时区不一致。

依赖:alembic、sqlalchemy、psycopg2(或 psycopg) 已安装;
     环境变量 POSTGRES_PASSWORD 已配置。

运行:
    python scripts/deploy_timezone.py

兼容:Windows / macOS / Linux。失败时非零退出,便于 CI 串联。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _ensure_project_root_on_syspath() -> None:
    """无论从哪里调用,都确保项目根目录在 sys.path。"""
    root = str(_PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _log(step: str, msg: str) -> None:
    print(f">> [{step}] {msg}", flush=True)


def _fail(msg: str) -> "None":
    print(f"!! {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# --------------------------------------------------------------------
# 0. 基础检查
# --------------------------------------------------------------------

def step_precheck() -> str:
    if not os.getenv("POSTGRES_PASSWORD"):
        _fail("需要设置 POSTGRES_PASSWORD 环境变量")

    _ensure_project_root_on_syspath()
    from config.settings import get_settings  # noqa: E402

    settings = get_settings()
    tz = settings.timezone
    _log("0/4", f"project root = {_PROJECT_ROOT}")
    _log("0/4", f"target timezone = {tz}")
    return tz


# --------------------------------------------------------------------
# 1. alembic upgrade head
# --------------------------------------------------------------------

def step_alembic_upgrade() -> None:
    _log("1/4", "running 'alembic upgrade head' ...")
    cmd = [sys.executable, "-m", "alembic", "upgrade", "head"]
    result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
    if result.returncode != 0:
        _fail(f"alembic upgrade head 失败 (exit={result.returncode})")


# --------------------------------------------------------------------
# 2. 会话时区校验
# --------------------------------------------------------------------

def step_verify_session_timezone(expected_tz: str) -> None:
    _log("2/4", "verifying SQLAlchemy / PostgresSaver session timezone ...")
    from config.settings import get_settings
    from core.database.engine import get_engine
    from sqlalchemy import text

    settings = get_settings()
    engine = get_engine(settings.postgresql)

    with engine.connect() as conn:
        tz = conn.execute(text("SHOW timezone")).scalar()
        now = conn.execute(text("SELECT now()")).scalar()
    print(f"     session timezone = {tz}")
    print(f"     SELECT now()     = {now}")
    if tz != expected_tz:
        _fail(f"expected session timezone {expected_tz!r}, got {tz!r}")

    # PostgresSaver conninfo 的 options 参数(URL 编码)
    conninfo = settings.postgresql.conninfo
    parsed = urlparse(conninfo)
    options = unquote(parse_qs(parsed.query).get("options", [""])[0])
    print(f"     conninfo options = {options!r}")
    if f"TimeZone={expected_tz}" not in options:
        _fail("conninfo 缺少 TimeZone 参数,PostgresSaver 连接会绕过配置")

    _log("2/4", "OK: SQLAlchemy engine + PostgresSaver conninfo 时区注入已生效")


# --------------------------------------------------------------------
# 3. 端到端写入回归
# --------------------------------------------------------------------

def step_roundtrip_write() -> None:
    _log("3/4", "round-trip write test ...")
    from config.settings import get_settings
    from core.database.engine import get_engine
    from sqlalchemy import text

    settings = get_settings()
    engine = get_engine(settings.postgresql)
    with engine.begin() as conn:
        conn.execute(text("CREATE TEMP TABLE _tz_probe (ts TIMESTAMPTZ)"))
        conn.execute(text("INSERT INTO _tz_probe VALUES (now())"))
        ts = conn.execute(text("SELECT ts FROM _tz_probe")).scalar()

    if ts is None or ts.utcoffset() is None:
        _fail(f"读回值 {ts!r} 无时区信息,列类型或会话配置异常")

    offset = ts.utcoffset()
    print(f"     inserted now() -> {ts} (utcoffset={offset})")
    if offset != timedelta(hours=8):
        _fail(f"期望 +08:00,实际 {offset};请检查 APP_TIMEZONE 配置")

    _log("3/4", "OK: 北京时间已正确落盘")


# --------------------------------------------------------------------
# 4. 收尾提示
# --------------------------------------------------------------------

_POST_NOTE = """
下一步:
  1. 重启 API 服务 (uvicorn / gunicorn),使新配置与连接池生效。
  2. 观察第一条新 trace 的 started_at,应带 '+08:00' 或落在北京时间。
  3. LangGraph PostgresSaver 会在下次请求时用新 conninfo 打开连接,
     旧连接(如有)会在池回收时自然替换。

回滚方案:
  - 代码回滚到上一个 tag。
  - 执行 'alembic downgrade -1' 把 trace 表列类型回退到 TIMESTAMP。
  - 历史数据保持 UTC 语义,与回滚前一致。
"""


def main() -> int:
    tz = step_precheck()
    step_alembic_upgrade()
    step_verify_session_timezone(tz)
    step_roundtrip_write()
    _log("4/4", "All checks passed.")
    print(_POST_NOTE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
