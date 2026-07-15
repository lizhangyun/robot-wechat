"""
联系人管理

负责从微信同步联系人/群到本地数据库，并提供查询、搜索、改备注等能力。
采用"内存缓存 + 数据库持久化"双层结构：
- 内存缓存：高频读取直接命中内存，避免重复查询数据库；
- 数据库：保证重启后数据不丢失，并提供复杂检索。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 独立运行支持：将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger
from sqlalchemy import String, Text, Boolean, DateTime, Integer, select, update, func, or_
from sqlalchemy.orm import Mapped, mapped_column

from database import Base, Database
from wechat.hook_interface import APICommand, WeChatHookInterface


# ====================================================================== #
#  ORM 模型
# ====================================================================== #
class Contact(Base):
    """联系人/群  ORM 模型。

    联系人与群统一存储，通过 ``is_group`` 区分。
    """

    __tablename__ = "contacts"

    wxid: Mapped[str] = mapped_column(String(64), primary_key=True, comment="wxid/群wxid")
    nickname: Mapped[str] = mapped_column(String(128), default="", comment="昵称")
    remark: Mapped[str] = mapped_column(String(128), default="", comment="备注名")
    avatar: Mapped[str] = mapped_column(Text, default="", comment="头像URL")
    is_group: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否群聊")
    group_name: Mapped[str] = mapped_column(String(128), default="", comment="群名(群聊)")
    member_count: Mapped[int] = mapped_column(Integer, default=0, comment="成员数(群聊)")
    alias: Mapped[str] = mapped_column(String(64), default="", comment="微信号")
    remark_quanpin: Mapped[str] = mapped_column(String(256), default="", comment="备注全拼(搜索用)")
    sync_time: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, comment="最后同步时间"
    )


# ====================================================================== #
#  联系人管理器
# ====================================================================== #
class ContactManager:
    """联系人管理器。

    Args:
        client: 微信客户端接口实例。
        db: 异步数据库管理器。
    """

    def __init__(self, client: WeChatHookInterface, db: Database) -> None:
        self.client: WeChatHookInterface = client
        self.db: Database = db
        # 内存缓存：wxid -> Contact 字典（含群与个人）
        self._cache: dict[str, Contact] = {}
        self._cache_ready: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  同步
    # ------------------------------------------------------------------ #
    async def sync_contacts(self) -> int:
        """从微信同步联系人与群到本地数据库。

        Returns:
            本次同步写入/更新的记录总数。
        """
        logger.info("开始同步联系人...")
        total = 0
        try:
            contacts = await self.client.get_contacts()
            groups = await self.client.get_groups()
            now = datetime.utcnow()

            async with self.db.session() as session:
                # 个人联系人
                for c in contacts:
                    wxid = c.get("wxid") or c.get("wxid", "")
                    if not wxid:
                        continue
                    await self._upsert_contact(
                        session,
                        wxid=wxid,
                        nickname=c.get("nickname", ""),
                        remark=c.get("remark", ""),
                        avatar=c.get("avatar", ""),
                        alias=c.get("alias", ""),
                        is_group=False,
                        sync_time=now,
                    )
                    total += 1
                # 群聊
                for g in groups:
                    gwxid = g.get("group_wxid") or g.get("wxid") or ""
                    if not gwxid:
                        continue
                    await self._upsert_contact(
                        session,
                        wxid=gwxid,
                        nickname=g.get("group_name", ""),
                        remark=g.get("remark", ""),
                        avatar=g.get("avatar", ""),
                        alias="",
                        is_group=True,
                        group_name=g.get("group_name", ""),
                        member_count=int(g.get("member_count", 0)),
                        sync_time=now,
                    )
                    total += 1
                await session.commit()

            # 同步后重建缓存
            await self._rebuild_cache()
            logger.info(f"联系人同步完成，共 {total} 条")
            return total
        except Exception as e:  # noqa: BLE001
            logger.exception(f"同步联系人失败: {e}")
            return total

    async def _upsert_contact(
        self, session: Any, wxid: str, **fields: Any
    ) -> None:
        """插入或更新一条联系人记录。"""
        existing = await session.get(Contact, wxid)
        if existing is None:
            session.add(Contact(wxid=wxid, **fields))
        else:
            for k, v in fields.items():
                setattr(existing, k, v)

    # ------------------------------------------------------------------ #
    #  查询
    # ------------------------------------------------------------------ #
    async def get_contact(self, wxid: str, use_cache: bool = True) -> Optional[dict[str, Any]]:
        """获取单个联系人信息。

        Args:
            wxid: 联系人 wxid。
            use_cache: 是否优先使用内存缓存。

        Returns:
            联系人字典，不存在返回 None。
        """
        if use_cache and self._cache_ready and wxid in self._cache:
            return self._contact_to_dict(self._cache[wxid])

        async with self.db.session() as session:
            contact = await session.get(Contact, wxid)
            if contact is None:
                return None
            data = self._contact_to_dict(contact)
            # 回填缓存
            self._cache[wxid] = contact
            return data

    async def search_contacts(
        self, keyword: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """搜索联系人。

        支持按 wxid、昵称、备注、微信号模糊匹配（不区分大小写）。

        Args:
            keyword: 搜索关键字。
            limit: 最多返回条数。

        Returns:
            联系人字典列表。
        """
        if not keyword:
            return []
        kw = f"%{keyword}%"
        async with self.db.session() as session:
            stmt = (
                select(Contact)
                .where(
                    or_(
                        Contact.wxid.ilike(kw),
                        Contact.nickname.ilike(kw),
                        Contact.remark.ilike(kw),
                        Contact.alias.ilike(kw),
                        Contact.group_name.ilike(kw),
                    )
                )
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [self._contact_to_dict(r) for r in rows]

    async def get_all_contacts(self, is_group: Optional[bool] = None) -> list[dict[str, Any]]:
        """获取全部联系人。

        Args:
            is_group: None=全部，True=仅群，False=仅个人。

        Returns:
            联系人字典列表。
        """
        async with self.db.session() as session:
            stmt = select(Contact)
            if is_group is not None:
                stmt = stmt.where(Contact.is_group == is_group)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [self._contact_to_dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    #  修改备注
    # ------------------------------------------------------------------ #
    async def update_remark(self, wxid: str, remark: str) -> bool:
        """修改联系人备注，并同步到微信。

        Args:
            wxid: 联系人 wxid。
            remark: 新备注名。

        Returns:
            是否修改成功。
        """
        try:
            # 1. 调用微信接口修改备注
            result = await self.client.api(
                APICommand.EDIT_REMARK, {"wxid": wxid, "remark": remark}
            )
            api_ok = isinstance(result, dict) and result.get("code") in (0, 200)

            # 2. 更新本地数据库（即使微信接口失败也尝试更新本地，便于离线）
            async with self.db.session() as session:
                contact = await session.get(Contact, wxid)
                if contact is None:
                    # 本地无记录则新建
                    session.add(
                        Contact(wxid=wxid, nickname="", remark=remark, is_group=False)
                    )
                else:
                    contact.remark = remark
                    contact.sync_time = datetime.utcnow()
                await session.commit()

            # 3. 更新缓存
            if wxid in self._cache:
                self._cache[wxid].remark = remark

            logger.info(
                f"修改备注 wxid={wxid} remark={remark} 微信接口={'成功' if api_ok else '失败(仅本地)'}"
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.exception(f"修改备注失败: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  缓存管理
    # ------------------------------------------------------------------ #
    async def _rebuild_cache(self) -> None:
        """从数据库重建内存缓存。"""
        async with self.db.session() as session:
            result = await session.execute(select(Contact))
            rows = result.scalars().all()
        self._cache = {r.wxid: r for r in rows}
        self._cache_ready = True
        logger.info(f"联系人缓存已重建，共 {len(self._cache)} 条")

    def get_cached(self, wxid: str) -> Optional[dict[str, Any]]:
        """从内存缓存获取联系人（不查数据库）。"""
        contact = self._cache.get(wxid)
        return self._contact_to_dict(contact) if contact else None

    async def get_display_name(self, wxid: str) -> str:
        """获取联系人显示名：优先备注 > 昵称 > wxid。"""
        data = await self.get_contact(wxid)
        if data is None:
            return wxid
        return data.get("remark") or data.get("nickname") or wxid

    async def cache_stats(self) -> dict[str, int]:
        """返回缓存统计信息。"""
        async with self.db.session() as session:
            total = await session.scalar(select(func.count()).select_from(Contact))
            groups = await session.scalar(
                select(func.count()).select_from(Contact).where(Contact.is_group == True)  # noqa: E712
            )
        return {
            "cache_size": len(self._cache),
            "db_total": int(total or 0),
            "db_groups": int(groups or 0),
        }

    # ------------------------------------------------------------------ #
    #  工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _contact_to_dict(contact: Contact) -> dict[str, Any]:
        """将 ORM 对象转为字典。"""
        return {
            "wxid": contact.wxid,
            "nickname": contact.nickname,
            "remark": contact.remark,
            "avatar": contact.avatar,
            "is_group": contact.is_group,
            "group_name": contact.group_name,
            "member_count": contact.member_count,
            "alias": contact.alias,
            "sync_time": contact.sync_time.isoformat() if contact.sync_time else "",
        }


# ====================================================================== #
#  独立运行测试（模拟模式）
# ====================================================================== #
async def _self_test() -> None:
    """模拟模式自测：同步联系人、查询、搜索、改备注。"""
    from wechat.wechat_client import WeChatClient

    client = WeChatClient(instance_id="test", mock=True)
    await client.init("test")
    await client.load_window()

    # 使用临时数据库
    db = Database(":memory:")
    await db.init()

    mgr = ContactManager(client, db)

    total = await mgr.sync_contacts()
    logger.info(f"同步 {total} 条")

    contact = await mgr.get_contact("wxid_test001")
    logger.info(f"查询 wxid_test001: {contact}")

    results = await mgr.search_contacts("张")
    logger.info(f"搜索 '张' 命中 {len(results)} 条: {[r['nickname'] for r in results]}")

    ok = await mgr.update_remark("wxid_test001", "新备注-张三")
    logger.info(f"改备注结果: {ok}")
    contact2 = await mgr.get_contact("wxid_test001")
    logger.info(f"改备注后: remark={contact2['remark']}")

    stats = await mgr.cache_stats()
    logger.info(f"缓存统计: {stats}")

    await db.close()
    await client.uninstall()
    logger.info("联系人管理自测完成")


if __name__ == "__main__":
    asyncio.run(_self_test())
