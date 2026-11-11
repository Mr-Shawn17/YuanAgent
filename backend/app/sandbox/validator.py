"""git 参数校验器（spec docs/sandbox-query.md §5）：固定子命令 + 每子命令选项白名单。

纯函数、零 I/O、零导入依赖：沙箱命令面唯一入口，规则宁可错杀。
argv 数组传递（无 shell、无重定向/管道拼接面）；校验通过原样返回 argv，
失败抛 GitArgumentError，错误文本面向模型可直接阅读。

git 七件套 + PR3 扩展（v1.1）：log/show/diff/blame/branch/tag/rev-parse
+ grep/shortlog/describe；rg（rg_query）走独立白名单 validate_rg_args。
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
        "-S": True,  # pickaxe：增删该字符串的提交
        "-G": True,  # pickaxe：diff 命中该正则的提交
    },
    "show": {"--stat": False, "--name-only": False, "--format": True},
    "diff": {"--stat": False, "--name-only": False},
    "blame": {"-L": True},
    "branch": {"-a": False, "--list": False, "--contains": True},
    "tag": {"--list": False, "--contains": True},
    "rev-parse": {"--short": False},
    # PR3 扩展（spec §5 v1.1）：搜索与统计
    "grep": {
        "-n": False,  # 行号
        "-i": False,  # 忽略大小写
        "-w": False,  # 全词匹配
        "-v": False,  # 反选
        "-E": False,  # 扩展正则
        "-F": False,  # 固定字符串
        "-e": True,  # 模式（可重复，等价第一个位置参数）
    },
    "shortlog": {
        "-s": False,  # 只统计条数
        "-n": False,  # 按条数排序
        "--since": True,
        "--until": True,
        "--author": True,
    },
    "describe": {"--tags": False, "--always": False, "--contains": True},
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
    "grep": (1, 3),  # <pattern> [path...]（首个位置参数为模式，其余为路径）
    "shortlog": (1, 1),  # <rev 或区间>（无参数会读 stdin，强制给出）
    "describe": (0, 1),  # [commit-ish]
}

# rg（PR3 rg_query）：选项白名单（spec §5 v1.1）。--pre 等可执行项不在白名单，
# 天然被拒；--color 等固定旗标由容器驱动层拼装，不经模型。
RG_OPTIONS: Final[dict[str, bool]] = {
    "-n": False,  # 行号
    "-i": False,  # 忽略大小写
    "-w": False,  # 全词匹配
    "-g": True,  # glob 过滤（可重复）
    "-t": True,  # 文件类型过滤
    "-m": True,  # 每文件最大命中数
    "-A": True,  # 后文 N 行
    "-B": True,  # 前文 N 行
    "-C": True,  # 上下文 N 行
}
_RG_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"^\d{1,6}$")  # -m/-A/-B/-C 的行数
_RG_MAX_POSITIONALS = 0  # 模式与路径由工具参数显式传递，不进白名单 argv

# --format 受限模板：仅允许日志格式占位符与安全标点，
# 排除引号/反引号/$/|/;&<> 等拼接面字符（argv 模式本无 shell，纵深防御）
_FORMAT_RE: Final[re.Pattern[str]] = re.compile(r"^[\w %(),:./\-+#@!?*=]*$")
_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"^\d{1,6}$")
# 支持连写取值的单横线选项（-L5,10 / -Sfoo / -Gfoo / -efoo / -A2 / -m5 / -tpy）
_ATTACHED_VALUE_OPTS: Final[frozenset[str]] = frozenset(
    {"-L", "-S", "-G", "-e", "-A", "-B", "-C", "-m", "-t", "-g"}
)


class GitArgumentError(ValueError):
    """参数校验失败：subcommand 不在白名单 / 选项越界 / 值非法 / 位置参数越界。"""


def _is_printable(text: str) -> bool:
    """拒绝控制字符（换行/回车/制表等）：防日志注入与畸形参数。"""
    return all(0x20 <= ord(char) <= 0x7E or ord(char) > 0x9F for char in text)


def _split_option(arg: str) -> tuple[str, str | None]:
    """把一个选项参数拆成 (选项名, 附带值)。

    --format=%h     → ("--format", "%h")
    -L5,10          → ("-L", "5,10")
    -Sfoo / -Gfoo / -efoo → ("-S"/"-G"/"-e", "foo")（单横线取值选项连写）
    --oneline       → ("--oneline", None)
    """
    if arg.startswith("--") and "=" in arg:
        name, _, value = arg.partition("=")
        return name, value
    # 单横线取值选项的连写形式（git blame -L5,10 / log -Sfoo / grep -efoo）
    if len(arg) > 2 and arg[:2] in _ATTACHED_VALUE_OPTS:
        return arg[:2], arg[2:]
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
    pattern_via_e = False  # grep 用 -e 提供模式时，位置参数只剩路径（动态区间）
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
            if name == "-e":
                pattern_via_e = True
            index += 1
            continue
        if index + 1 >= len(args):
            raise GitArgumentError(f"选项 {name} 缺少值（末尾悬空）")
        value = args[index + 1]
        if value.startswith("-"):
            raise GitArgumentError(f"选项 {name} 的值非法（不能以 - 开头）：{value[:32]}")
        _validate_value(name, value)
        if name == "-e":
            pattern_via_e = True
        index += 2

    low, high = _POSITIONAL_RANGE[subcommand]
    if subcommand == "grep" and pattern_via_e:
        low, high = 0, 2  # -e 已提供模式，位置参数只剩可选路径
    if not low <= len(positionals) <= high:
        expect = f"{low}" if low == high else f"{low}~{high}"
        raise GitArgumentError(
            f"子命令 {subcommand} 需要 {expect} 个位置参数，收到 {len(positionals)}"
        )
    return args


def validate_rg_args(argv: list[str]) -> list[str]:
    """校验 rg 检索的选项参数（spec §5 v1.1），通过则原样返回。

    与 git 校验同一套纪律：白名单选项、值长 256、总长 4000、拒绝控制字符。
    模式与路径不进本 argv（由 rg_query 工具参数显式传递，驱动层以 ``--`` 分隔，
    防止以 ``-`` 开头的模式被解析为选项）；行数类值（-m/-A/-B/-C）限纯数字。
    """
    if not argv:
        return []
    for arg in argv:
        if not isinstance(arg, str) or not arg:
            raise GitArgumentError("argv 必须是非空字符串数组")
    if sum(len(arg) for arg in argv) > MAX_TOTAL_CHARS:
        raise GitArgumentError(f"参数总长超过 {MAX_TOTAL_CHARS} 字符上限")

    index = 0
    while index < len(argv):
        arg = argv[index]
        if not arg.startswith("-"):
            raise GitArgumentError(
                f"rg 选项列表不允许位置参数：{arg[:32]}（模式/路径请用工具参数传递）"
            )
        if arg.startswith("--"):
            raise GitArgumentError(f"rg 不允许长选项：{arg[:48]}")
        name, attached = _split_option(arg)
        if name not in RG_OPTIONS:
            raise GitArgumentError(f"rg 不允许选项：{name}")
        if RG_OPTIONS[name] is False:
            if attached is not None:
                raise GitArgumentError(f"选项 {name} 不接受值")
            index += 1
            continue
        if attached is not None:
            value = attached
            consumed_next = False
        else:
            if index + 1 >= len(argv):
                raise GitArgumentError(f"选项 {name} 缺少值（末尾悬空）")
            value = argv[index + 1]
            consumed_next = True
        if len(value) > MAX_VALUE_CHARS:
            raise GitArgumentError(f"选项 {name} 的值过长（>{MAX_VALUE_CHARS} 字符）")
        if not _is_printable(value):
            raise GitArgumentError(f"选项 {name} 的值含控制字符")
        if name in ("-m", "-A", "-B", "-C") and not _RG_VALUE_RE.fullmatch(value):
            raise GitArgumentError(f"选项 {name} 的值必须是非负整数，收到：{value[:32]}")
        index += 2 if consumed_next else 1
    return list(argv)


def validate_rg_path(path: str) -> str:
    """校验 rg 的可选路径限定，返回归一化（正斜杠）路径；空串表示全仓库检索。

    rg 不像 git 会自行拒绝仓库外 pathspec：``../`` 可读到容器 rootfs（/etc 等）。
    因此这里拦死：绝对路径（/ 开头、盘符）、``..`` 穿越、反斜杠、以 - 开头。
    """
    text = path.strip()
    if not text:
        return ""
    if len(text) > MAX_VALUE_CHARS:
        raise GitArgumentError(f"路径过长（>{MAX_VALUE_CHARS} 字符）")
    if not _is_printable(text):
        raise GitArgumentError("路径含控制字符")
    if text.startswith(("/", "\\")) or ":" in text[:3]:
        raise GitArgumentError(f"路径必须是仓库内相对路径，拒绝绝对路径：{text[:64]}")
    if "\\" in text:
        raise GitArgumentError(f"路径请使用正斜杠：{text[:64]}")
    if text.startswith("-"):
        raise GitArgumentError(f"路径不能以 - 开头：{text[:64]}")
    if any(part == ".." for part in text.split("/")):
        raise GitArgumentError(f"路径不允许 .. 穿越：{text[:64]}")
    return text


__all__ = [
    "GLOBAL_REJECTED",
    "MAX_TOTAL_CHARS",
    "MAX_VALUE_CHARS",
    "RG_OPTIONS",
    "GitArgumentError",
    "validate_git_args",
    "validate_rg_args",
    "validate_rg_path",
]
