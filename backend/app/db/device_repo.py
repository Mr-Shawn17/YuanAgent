"""devices 表读写入口：设备令牌的签发、校验、吊销与活跃时间维护。

令牌原文格式 yrt_<urlsafe-43>，仅签发时返回一次；库内只存 SHA-256 十六进制。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Device, utcnow, uuid7

# 令牌前缀（YuanAgent Runner Token），便于一眼识别泄露来源
TOKEN_PREFIX = "yrt_"
# 秒级时间戳偏差容忍：touch_seen 去抖，避免每条消息都写库
SEEN_THROTTLE = timedelta(seconds=60)


def generate_token() -> str:
    """生成设备令牌原文（只显示一次）。"""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """令牌哈希：sha256 十六进制（64 字符，与表列宽一致）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class IssuedToken:
    """一次签发的完整结果：记录 + 令牌原文（不落库）。"""

    device: Device
    token: str


class DeviceRepo:
    """devices 表的读写入口（只 flush 不 commit，事务边界在 Database.session）。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(self, *, user_id: str, name: str = "") -> IssuedToken:
        """签发新设备：生成令牌原文与哈希，哈希落库，原文只返回一次。"""
        token = generate_token()
        record = Device(
            id=uuid7(),
            user_id=user_id,
            name=name.strip()[:64],
            token_hash=hash_token(token),
        )
        self._db.add(record)
        await self._db.flush()
        return IssuedToken(device=record, token=token)

    async def get_by_token(self, token: str) -> Device | None:
        """按令牌原文查有效设备；不存在或已吊销返回 None。"""
        stmt = select(Device).where(
            Device.token_hash == hash_token(token),
            Device.revoked.is_(False),
        )
        return (await self._db.scalars(stmt)).first()

    async def get(self, device_id: str) -> Device | None:
        return await self._db.get(Device, device_id)

    async def list_devices(self, *, include_revoked: bool = True) -> list[Device]:
        stmt = select(Device).order_by(Device.created_at.desc())
        if not include_revoked:
            stmt = stmt.where(Device.revoked.is_(False))
        return list((await self._db.scalars(stmt)).all())

    async def revoke(self, device_id: str) -> Device | None:
        """吊销设备：令牌立即失效；不存在时返回 None。"""
        record = await self.get(device_id)
        if record is None:
            return None
        record.revoked = True
        await self._db.flush()
        return record

    async def touch_seen(self, device_id: str) -> None:
        """更新活跃时间（去抖：60 秒内重复调用不写库）。"""
        record = await self.get(device_id)
        if record is None:
            return
        now = utcnow()
        if record.last_seen_at is not None and now - record.last_seen_at < SEEN_THROTTLE:
            return
        await self._db.execute(
            update(Device).where(Device.id == device_id).values(last_seen_at=now)
        )
        await self._db.flush()


__all__ = [
    "TOKEN_PREFIX",
    "DeviceRepo",
    "IssuedToken",
    "generate_token",
    "hash_token",
]
