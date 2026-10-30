from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import BaseTool, tool

from app.tools.base import ToolContext, ToolEntryConfig, tool_error_boundary

DEFAULT_TIMEZONE = "Asia/Shanghai"
WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def make_time_tool(context: ToolContext, entry: ToolEntryConfig) -> BaseTool:
    @tool("get_current_time")
    @tool_error_boundary
    def get_current_time(timezone: str = "") -> str:
        """获取当前的日期、时间与星期，回答"现在几点/今天几号"类问题时使用。

        Args:
            timezone: IANA 时区名（如 Asia/Shanghai、America/New_York），留空默认 Asia/Shanghai。
        """
        name = timezone.strip() or DEFAULT_TIMEZONE
        try:
            tz = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return f"未知的时区：{name}，请使用 IANA 时区名，例如 Asia/Shanghai"
        now = datetime.now(tz)
        weekday = WEEKDAY_NAMES[now.weekday()]
        return f"当前时间：{now:%Y-%m-%d %H:%M:%S} {weekday}（时区 {name}，UTC{now:%z}）"

    return get_current_time


__all__ = ["make_time_tool"]
