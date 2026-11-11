"""git 参数校验器（spec docs/sandbox-query.md §5）：固定子命令 + 每子命令选项白名单。

纯函数、零 I/O、零导入依赖：沙箱命令面唯一入口，规则宁可错杀。
argv 数组传递（无 shell、无重定向/管道拼接面）；校验通过原样返回 argv，
失败抛 GitArgumentError，错误文本面向模型可直接阅读。

七件套（MVP 命令面，见 spec §5 表格）：
    log / show / diff / blame / branch / tag / rev-parse
"""

from __future__ import annotations

import re
from typing import Final

MAX_VALUE_CHARS: Final[int] = 256  # 单个选项值/位置参数长度上限（spec §5）
MAX_TOTAL_CHARS: Final[int] = 4000  # argv 参数总长上限（spec §5）

# 全局拒绝项（spec §5）：一切可注入执行/改写仓库路径的选项。
# 白名单之外的选项本来就会被拒，这里显式列出是为了错误信息更可读（双保险）。
GLOBAL_REJECTED: Final[frozenset[str]] = frozenset(
    {
        "-c",
        "--exec-path",
        "--namespace",
        "--upload-pack",
        "--receive-pack",
        "--git-dir",
        "--work-tree",
        "--super-prefix",
        "--no-replace-objects",
        "--bare",
        "--html-path",
        "--man-path",
        "--info-path",
    }
)
_CONFIG_PREFIX = "--config"  # --config / --config-env / --config=k=v 前缀族

# 每子命令选项白名单：选项名 → 是否取值
_OPTIONS: Final[dict[str, dict[str, bool]]] = {
    "log": {
        "--oneline": False,
        "--graph": False,
        "--all": False,
        "--max-count": True,
        "--since": True,
        "--until": True,
        "--author": True,
        "--format": True,
    },
    "show": {"--stat": False, "--name-only": False, "--format": True},
    "diff": {"--stat": False, "--name-only": False},
    "blame": {"-L": True},
    "branch": {"-a": False, "--list": False, "--contains": True},
    "tag": {"--list": False, "--contains": True},
    "rev-parse": {"--short": False},
}

# 每子命令位置参数数量区间 (min, max)（spec §5 表格：<ref...> 与可选 [path]/[pattern]）
_POSITIONAL_RANGE: Final[dict[str, tuple[int, int]]] = {
    "log": (0, 0),
    "show": (1, 2),  # <commit-ish> [path]
    "diff": (2, 3),  # <ref1> <ref2> [path]
    "blame": (2, 2),  # <rev> <path>
    "branch": (0, 0),
    "tag": (0, 1),  # [pattern]
    "rev-parse": (1, 1),  # HEAD / <ref>
}

# --format 受限模板：仅允许日志格式占位符与安全标点，
# 排除引号/反引号/$/|/;&<> 等拼接面字符（argv 模式本无 shell，纵深防御）
_FORMAT_RE: Final[re.Pattern[str]] = re.compile(r"^[\w %(),:./\-+#@!?*=]*$")
_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"^\d{1,6}$")


class GitArgumentError(ValueError):
    """参数校验失败：subcommand 不在白名单 / 选项越界 / 值非法 / 位置参数越界。"""


def _is_printable(text: str) -> bool:
    """拒绝控制字符（换行/回车/制表等）：防日志注入与畸形参数。"""
    return all(0x20 <= ord(char) <= 0x7E or ord(char) > 0x9F for char in text)


def _split_option(arg: str) -> tuple[str, str | None]:
    """把一个选项参数拆成 (选项名, 附带值)。

    --format=%h     → ("--format", "%h")
    -L5,10          → ("-L", "5,10")
    --oneline       → ("--oneline", None)
    """
    if arg.startswith("--") and "=" in arg:
        name, _, value = arg.partition("=")
        return name, value
    # 单短横线取值选项的连写形式（git blame -L5,10）
    if len(arg) > 2 and arg.startswith("-L"):
        return "-L", arg[2:]
    return arg, None


def _validate_value(option: str, value: str) -> None:
    """校验选项值：长度、控制字符、特定选项的格式约束。"""
    if len(value) > MAX_VALUE_CHARS:
        raise GitArgumentError(f"选项 {option} 的值过长（>{MAX_VALUE_CHARS} 字符）")
    if not _is_printable(value):
        raise GitArgumentError(f"选项 {option} 的值含控制字符")
    if option == "--format" and not _FORMAT_RE.fullmatch(value):
        raise GitArgumentError(
            f"--format 模板含受限字符：{value[:32]}…"
            if len(value) > 32
            else f"--format 模板含受限字符：{value}"
        )
    if option == "--max-count" and not _DIGITS_RE.fullmatch(value):
        raise GitArgumentError(f"--max-count 必须是非负整数，收到：{value[:32]}")


def validate_git_args(subcommand: str, argv: list[str]) -> list[str]:
    """校验沙箱 git 命令的子命令与参数（spec §5），通过则原样返回 argv 列表。

    校验维度：子命令白名单 → 全局拒绝项 → 每子命令选项白名单（取值选项的
    值一并校验）→ 位置参数数量与字符约束 → 值长度/总长上限。
    支持两种取值形式：``--opt value`` 与 ``--opt=value``（连写 ``-L5,10`` 仅 -L）。
    """
    if subcommand not in _OPTIONS:
        allowed = "、".join(sorted(_OPTIONS))
        raise GitArgumentError(f"不支持的子命令：{subcommand}，可用：{allowed}")

    args = list(argv)
    for arg in args:
        if not isinstance(arg, str) or not arg:
            raise GitArgumentError("argv 必须是非空字符串数组")
    if sum(len(arg) for arg in args) > MAX_TOTAL_CHARS:
        raise GitArgumentError(f"参数总长超过 {MAX_TOTAL_CHARS} 字符上限")

    allowed_opts = _OPTIONS[subcommand]
    positionals: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if not arg.startswith("-"):
            # 位置参数：commit-ish / ref / path / pattern
            if len(arg) > MAX_VALUE_CHARS:
                raise GitArgumentError(f"位置参数过长（>{MAX_VALUE_CHARS} 字符）")
            if not _is_printable(arg):
                raise GitArgumentError("位置参数含控制字符")
            positionals.append(arg)
            index += 1
            continue

        name, attached = _split_option(arg)
        if name in GLOBAL_REJECTED or name.startswith(_CONFIG_PREFIX):
            raise GitArgumentError(f"全局拒绝的选项：{name}")
        if name not in allowed_opts:
            raise GitArgumentError(f"子命令 {subcommand} 不允许选项：{name}")
        if allowed_opts[name] is False:
            if attached is not None:
                raise GitArgumentError(f"选项 {name} 不接受值")
            index += 1
            continue

        # 取值选项：附带形式（--opt=v / -Lv）或紧随其后的下一个参数
        if attached is not None:
            _validate_value(name, attached)
            index += 1
            continue
        if index + 1 >= len(args):
            raise GitArgumentError(f"选项 {name} 缺少值（末尾悬空）")
        value = args[index + 1]
        if value.startswith("-"):
            raise GitArgumentError(f"选项 {name} 的值非法（不能以 - 开头）：{value[:32]}")
        _validate_value(name, value)
        index += 2

    low, high = _POSITIONAL_RANGE[subcommand]
    if not low <= len(positionals) <= high:
        expect = f"{low}" if low == high else f"{low}~{high}"
        raise GitArgumentError(
            f"子命令 {subcommand} 需要 {expect} 个位置参数，收到 {len(positionals)}"
        )
    return args


__all__ = [
    "GLOBAL_REJECTED",
    "MAX_TOTAL_CHARS",
    "MAX_VALUE_CHARS",
    "GitArgumentError",
    "validate_git_args",
]
