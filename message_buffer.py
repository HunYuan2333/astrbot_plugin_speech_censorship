"""消息缓冲管理模块 - 负责消息的累积、同步和状态管理"""

import asyncio
import copy
import time
from collections import defaultdict
from typing import Dict, List


class MessageBuffer:
    """消息缓冲区管理器

    职责：
    - 管理每个群组的消息缓冲区
    - 提供线程安全的缓冲区访问（通过 asyncio 锁）
    - 支持消息的原子提取和缓冲区重置
    - 清理过期消息
    """

    def __init__(self):
        # 消息缓冲区：{group_id: {user_id: [{"message": str, "timestamp": int, "user_name": str}]}}
        self.buffer: Dict[str, Dict[str, List[Dict]]] = defaultdict(
            lambda: defaultdict(list)
        )

        # 每个群组的最后检测时间
        self.last_check_time: Dict[str, float] = {}

        # 每个群组的并发锁（防止竞争）
        self.group_locks: Dict[str, asyncio.Lock] = {}

        # 保护 group_locks 字典本身的锁（修复非原子性竞态）
        self._locks_dict_lock = asyncio.Lock()

    async def get_or_create_lock(self, group_id: str) -> asyncio.Lock:
        """获取或创建群组的锁（原子操作，P0修复：移除快速路径避免竞态）"""
        # P0 修复：所有操作都在锁内进行，避免快速路径的竞态条件
        async with self._locks_dict_lock:
            if group_id not in self.group_locks:
                self.group_locks[group_id] = asyncio.Lock()
            return self.group_locks[group_id]

    def _cleanup_empty_lock(self, group_id: str):
        """保留锁引用，避免等待中的协程拿到不同锁实例导致互斥失效。"""
        return

    def append_message(self, group_id: str, user_id: str, message: dict):
        """向缓冲区添加消息（调用方应在协程上下文中确保顺序访问）"""
        self.buffer[group_id][user_id].append(message)

    async def append_message_with_lock(self, group_id: str, user_id: str, message: dict):
        """在群组锁保护下添加消息"""
        lock = await self.get_or_create_lock(group_id)
        async with lock:
            self.append_message(group_id, user_id, message)

    def get_total_messages(self, group_id: str) -> int:
        """获取群组的总消息数"""
        if group_id not in self.buffer:
            return 0
        return sum(len(msgs) for msgs in self.buffer[group_id].values())

    def get_group_ids_snapshot(self) -> List[str]:
        """获取群组ID快照，避免迭代期间字典被并发修改"""
        return list(self.buffer.keys())

    def snapshot_and_clear(self, group_id: str) -> Dict[str, List[Dict]]:
        """原子操作：创建快照并清空缓冲区

        这是解决竞态条件的关键：在锁保护下，
        创建消息副本并立即清空缓冲区，
        这样新消息可以在 LLM 处理期间继续写入新的缓冲区。
        """
        if group_id not in self.buffer:
            return {}

        # 深拷贝快照（包含消息中可能存在的嵌套结构）
        snapshot: Dict[str, List[Dict]] = copy.deepcopy(dict(self.buffer[group_id]))

        # 立即清空缓冲区，允许新消息写入
        self.buffer[group_id].clear()

        return snapshot

    def cleanup_old_messages(self, max_age_seconds: int = 3600):
        """清理超过 max_age_seconds 的旧消息

        Args:
            max_age_seconds: 消息最大保留秒数（默认 3600 秒）
        """
        current_time = time.time()
        cutoff_time = current_time - max_age_seconds

        for group_id in list(self.buffer.keys()):
            for user_id in list(self.buffer[group_id].keys()):
                # 过滤掉旧消息
                valid_messages = []
                for msg in self.buffer[group_id][user_id]:
                    raw_timestamp = msg.get("timestamp", 0)
                    try:
                        timestamp_value = float(raw_timestamp)
                    except (TypeError, ValueError):
                        timestamp_value = 0.0

                    if timestamp_value >= cutoff_time:
                        valid_messages.append(msg)

                self.buffer[group_id][user_id] = valid_messages

                # 如果该用户没有消息了，删除该用户
                if not self.buffer[group_id][user_id]:
                    del self.buffer[group_id][user_id]

            # 如果该群没有消息了，删除该群并清理其锁
            if not self.buffer[group_id]:
                del self.buffer[group_id]
                self._cleanup_empty_lock(group_id)
                self.last_check_time.pop(group_id, None)

    def trim_recent_messages(self, group_id: str, limit: int):
        """仅保留某群最近 N 条消息（跨用户全局窗口）。limit<=0 时不限制。"""
        if limit <= 0 or group_id not in self.buffer:
            return

        # 统计总消息数
        total_count = sum(len(msgs) for msgs in self.buffer[group_id].values())
        if total_count <= limit:
            return

        # 扁平化并排序（修复：规范化 timestamp 类型，防止比较异常）
        flattened = []
        for uid, messages in self.buffer[group_id].items():
            for msg in messages:
                raw_timestamp = msg.get("timestamp", 0)
                try:
                    timestamp_value = float(raw_timestamp)
                except (TypeError, ValueError):
                    timestamp_value = 0.0
                flattened.append((timestamp_value, uid, msg))

        flattened.sort(key=lambda item: item[0])
        recent_items = flattened[-limit:]

        # 重建缓冲区
        rebuilt = defaultdict(list)
        for _, uid, msg in recent_items:
            rebuilt[uid].append(msg)

        self.buffer[group_id] = rebuilt

    def restore_snapshot(self, group_id: str, snapshot: Dict[str, List[Dict]], limit: int = 0):
        """将处理失败的快照回灌到缓冲区。

        调用方需在群组锁保护下调用，避免并发写入冲突。
        回灌后可按 limit 执行全局窗口裁剪。
        """
        if not snapshot:
            return

        if group_id not in self.buffer:
            self.buffer[group_id] = defaultdict(list)

        # 回灌时按“旧消息在前，新消息在后”合并，避免时间顺序错乱
        for user_id, old_messages in snapshot.items():
            current_messages = self.buffer[group_id].get(user_id, [])
            self.buffer[group_id][user_id] = list(old_messages) + list(current_messages)

        if limit > 0:
            self.trim_recent_messages(group_id, limit)

    def update_check_time(self, group_id: str, timestamp: float = None):
        """更新群组的最后检测时间"""
        if timestamp is None:
            timestamp = time.time()
        self.last_check_time[group_id] = timestamp

    def get_check_time(self, group_id: str) -> float:
        """获取群组的最后检测时间"""
        return self.last_check_time.get(group_id, 0)

    def ensure_check_time_initialized(self, group_id: str):
        """确保群组的检测时间已初始化"""
        if group_id not in self.last_check_time:
            self.last_check_time[group_id] = time.time()
