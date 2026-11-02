"""生成周报骨架：按 ISO 周号计算起止日期，创建空白模板文件。

用法：
    python gen_skeleton.py [--week 2026-W44] [--output weekly_report.md]

只负责创建文件，内容由 Agent 根据用户口述填充（见 SKILL.md 第三步）。
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path


def iso_week_range(week_text: str) -> tuple[date, date]:
    """把 ISO 周号（如 2026-W44）解析为该周的周一与周日。"""
    year, week = week_text.split("-W")
    start = datetime.fromisocalendar(int(year), int(week), 1).date()
    return start, start + timedelta(days=6)


def build_skeleton(start: date, end: date) -> str:
    fmt = "%Y-%m-%d"
    return f"""# 周报（{start.strftime(fmt)} ~ {end.strftime(fmt)}）

## 本周完成
- 

## 进行中
- 

## 问题与风险
- 

## 下周计划
- 
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="生成周报骨架文件")
    parser.add_argument(
        "--week",
        default=None,
        help="ISO 周号，如 2026-W44（缺省为当前周）",
    )
    parser.add_argument(
        "--output",
        default="weekly_report.md",
        help="输出文件名（默认 weekly_report.md）",
    )
    args = parser.parse_args()

    if args.week:
        start, end = iso_week_range(args.week)
    else:
        today = date.today()
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)

    output = Path(args.output)
    output.write_text(build_skeleton(start, end), encoding="utf-8")
    print(f"骨架已生成：{output.resolve()}（{start} ~ {end}）")


if __name__ == "__main__":
    main()
