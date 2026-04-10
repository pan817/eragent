"""密码加密工具（Fernet 对称加密）。

设计原则：
- 使用 ``cryptography.fernet``（AES-128-CBC + HMAC-SHA256），业界标准库。
- 主密钥（master key）**只通过环境变量 ``ENCRYPTION_MASTER_KEY`` 传入**，不落磁盘。
- 密文（ciphertext）可安全存入 ``.env`` 或配置文件；无主密钥则无法还原。
- 本模块无任何全局状态，所有函数均为纯函数，可在测试中随意调用。

CLI 用法（三条命令）::

    # 1. 生成主密钥（首次部署时执行一次，妥善保存输出值）
    python -m config.crypto generate-key

    # 2. 加密凭证（从环境变量读取待加密的明文，主密钥从 ENCRYPTION_MASTER_KEY 读取）
    #    示例：先 export POSTGRES_PASSWORD=devpass，再执行：
    ENCRYPTION_MASTER_KEY=<key> python -m config.crypto encrypt POSTGRES_PASSWORD

    # 3. 验证密文可正确解密（主密钥从 ENCRYPTION_MASTER_KEY 读取）
    ENCRYPTION_MASTER_KEY=<key> python -m config.crypto decrypt <ciphertext>

错误处理：
- 主密钥格式错误（非合法 Fernet key）→ ``ValueError``
- 密文被篡改或密钥不匹配 → ``InvalidTokenError``（由 cryptography 抛出）
- 指定的环境变量不存在或为空 → ``ValueError``
"""
from __future__ import annotations

import os
import sys

from cryptography.fernet import Fernet, InvalidToken


# ---------------------------------------------------------------------------
# 核心 API
# ---------------------------------------------------------------------------


def generate_master_key() -> str:
    """生成一个新的 Fernet 主密钥（Base64URL 编码，44 字符）。

    Returns:
        可直接赋值给 ``ENCRYPTION_MASTER_KEY`` 的字符串。
    """
    return Fernet.generate_key().decode()


def encrypt(plaintext: str, master_key: str) -> str:
    """用主密钥加密明文，返回 Fernet token（Base64URL 字符串）。

    Args:
        plaintext: 待加密的明文字符串（如数据库密码）。
        master_key: Fernet 主密钥字符串（由 ``generate_master_key()`` 生成）。

    Returns:
        加密后的密文字符串，可安全存入配置文件或环境变量。

    Raises:
        ValueError: master_key 格式无效（非合法 Fernet key）。
    """
    try:
        f = Fernet(master_key.encode())
    except (ValueError, Exception) as exc:
        raise ValueError(f"ENCRYPTION_MASTER_KEY 格式无效: {exc}") from exc
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str, master_key: str) -> str:
    """用主密钥解密密文，返回原始明文。

    Args:
        ciphertext: 由 ``encrypt()`` 生成的密文字符串。
        master_key: 加密时使用的 Fernet 主密钥字符串。

    Returns:
        原始明文字符串。

    Raises:
        ValueError: master_key 格式无效。
        InvalidToken: 密文被篡改、格式错误或密钥不匹配。
    """
    try:
        f = Fernet(master_key.encode())
    except (ValueError, Exception) as exc:
        raise ValueError(f"ENCRYPTION_MASTER_KEY 格式无效: {exc}") from exc
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise InvalidToken("密文解密失败：密钥不匹配或密文已被篡改")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def _cmd_generate_key() -> None:
    key = generate_master_key()
    print(key)
    print(
        "\n请将上方密钥保存到安全存储中，并通过环境变量注入：",
        file=sys.stderr,
    )
    print("  export ENCRYPTION_MASTER_KEY=<上方输出的密钥>", file=sys.stderr)


def _cmd_encrypt(env_var_name: str) -> None:
    master_key = os.environ.get("ENCRYPTION_MASTER_KEY", "")
    if not master_key:
        print(
            "错误：未设置 ENCRYPTION_MASTER_KEY 环境变量。",
            file=sys.stderr,
        )
        sys.exit(1)

    plaintext = os.environ.get(env_var_name, "")
    if not plaintext:
        print(
            f"错误：环境变量 {env_var_name!r} 未设置或为空。",
            file=sys.stderr,
        )
        sys.exit(1)

    ciphertext = encrypt(plaintext, master_key)
    encrypted_key = f"{env_var_name}_ENCRYPTED"
    print(ciphertext)
    print(f"\n将以下行写入 .env（并删除 {env_var_name}= 那行）：", file=sys.stderr)
    print(f"  {encrypted_key}={ciphertext}", file=sys.stderr)


def _cmd_decrypt(ciphertext: str) -> None:
    master_key = os.environ.get("ENCRYPTION_MASTER_KEY", "")
    if not master_key:
        print(
            "错误：未设置 ENCRYPTION_MASTER_KEY 环境变量。",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        plaintext = decrypt(ciphertext, master_key)
    except (InvalidToken, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)

    print(plaintext)


def _usage() -> None:
    print(
        "用法:\n"
        "  python -m config.crypto generate-key\n"
        "  python -m config.crypto encrypt <ENV_VAR_NAME>\n"
        "  python -m config.crypto decrypt <CIPHERTEXT>",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        _usage()
        sys.exit(1)

    cmd = args[0]
    if cmd == "generate-key":
        _cmd_generate_key()
    elif cmd == "encrypt":
        if len(args) < 2:
            print("错误：encrypt 需要指定环境变量名，例如：encrypt POSTGRES_PASSWORD", file=sys.stderr)
            sys.exit(1)
        _cmd_encrypt(args[1])
    elif cmd == "decrypt":
        if len(args) < 2:
            print("错误：decrypt 需要指定密文", file=sys.stderr)
            sys.exit(1)
        _cmd_decrypt(args[1])
    else:
        print(f"错误：未知命令 {cmd!r}", file=sys.stderr)
        _usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
