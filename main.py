"""群聊消息审核与自动禁言插件 - 主模块

这是简化后的主插件文件，负责：
1. 事件监听和路由
2. 模块之间的协调
3. 触发条件判断和定时任务管理
"""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools

from .message_buffer import MessageBuffer
from .llm_analyzer import LLMAnalyzer
from .violation_manager import ViolationManager
from .ban_executor import BanExecutor


class SpeechCensorshipPlugin(Star):
    """群聊消息审核与自动禁言插件

    架构：
    - MessageBuffer: 消息缓冲管理
    - LLMAnalyzer: LLM 分析和响应解析
    - ViolationManager: 违规记录持久化
    - BanExecutor: 禁言执行和警告
    - SpeechCensorshipPlugin: 事件处理和业务协调
    """

    REQUIRED_JSON_FORMAT = (
        '{"violations":[{"user_id":"123456","reason":"阴阳怪气/争吵/敏感话题"}]}'
    )

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        # 模块初始化
        self.message_buffer = MessageBuffer()
        self.llm_analyzer = LLMAnalyzer(context)

        # 数据目录
        self.data_dir = self._get_data_dir()

        # 违规管理器（延迟初始化在 initialize 中）
        self.violation_manager: Optional[ViolationManager] = None

        # 禁言执行器
        self.ban_executor = BanExecutor(
            self._try_get_aiocqhttp_event_class,
            self._get_config
        )

        # 定时检测任务
        self.timer_task: Optional[asyncio.Task] = None

        # 保存最新的 event 对象（用于发送消息和调用 API）
        self.latest_events: Dict[str, AstrMessageEvent] = {}

        # 缓存的导入（用于防止平台耦合）
        self._aiocqhttp_event_class = None

        logger.info("群聊消息审核插件已加载")

    def _get_config(self, key: str, default: Any = None) -> Any:
        """获取插件配置"""
        return self.config.get(key, default)

    def _try_get_aiocqhttp_event_class(self):
        """延迟加载平台特定类，避免硬编码耦合"""
        if self._aiocqhttp_event_class is not None:
            return self._aiocqhttp_event_class

        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            self._aiocqhttp_event_class = AiocqhttpMessageEvent
            return self._aiocqhttp_event_class
        except ImportError:
            logger.warning("无法导入 AiocqhttpMessageEvent，QQ平台特定功能将不可用")
            return None

    def _get_data_dir(self) -> Path:
        """获取数据保存目录（使用 AstrBot 的标准数据目录）

        必须遵循 AstrBot 框架标准：data/plugin_data/<plugin_name>
        """
        try:
            # 根据 AstrBot 框架规范，使用 StarTools.get_data_dir()
            data_dir = StarTools.get_data_dir()
            return Path(data_dir)
        except Exception as e:
            logger.error(f"获取数据目录失败: {e}")
            # 在极端情况下，仍然抛出异常而不是降级，以防止配置隐式失败
            raise

    async def initialize(self):
        """插件初始化：加载配置、启动定时任务"""
        trigger_mode = self._get_config("trigger_mode", "hybrid")
        batch_size = self._get_config("batch_size", 10)
        llm_provider = self._get_config("llm_provider", "")

        # 初始化违规管理器并加载历史记录
        self.violation_manager = ViolationManager(self.data_dir)
        await self.violation_manager.load_records()

        # 如果触发模式包含时间触发，启动定时器
        if trigger_mode in ["time_only", "hybrid", "strict_hybrid"]:
            self.timer_task = asyncio.create_task(self._periodic_check())
            check_interval = self._get_config("check_interval", 60)
            logger.info(f"定时检测任务已启动（间隔: {check_interval} 秒，模式: {trigger_mode}）")

        logger.info(
            f"当前配置：trigger_mode={trigger_mode}, batch_size={batch_size}, llm_provider={llm_provider or '未配置'}"
        )
        logger.info("群聊消息审核插件初始化完成")

    @filter.command("censor_status")
    async def censor_status(self, event: AstrMessageEvent):
        """查看当前审核配置状态"""
        trigger_mode = self._get_config("trigger_mode", "hybrid")
        check_interval = self._get_config("check_interval", 60)
        batch_size = self._get_config("batch_size", 10)
        recent_message_limit = self._get_config("recent_message_limit", 50)
        llm_provider = self._get_config("llm_provider", "")

        total_groups = len(self.message_buffer.buffer)
        total_messages = sum(
            sum(len(msgs) for msgs in users.values())
            for users in self.message_buffer.buffer.values()
        ) if total_groups > 0 else 0

        stats = self.violation_manager.get_stats() if self.violation_manager else {}

        yield event.plain_result(
            "审核状态:\n"
            f"- trigger_mode: {trigger_mode}\n"
            f"- check_interval: {check_interval}\n"
            f"- batch_size: {batch_size}\n"
            f"- recent_message_limit: {recent_message_limit}\n"
            f"- llm_provider: {llm_provider or '未配置'}\n"
            f"- buffer_groups: {total_groups}\n"
            f"- buffer_messages: {total_messages}\n"
            f"- violation_records: {stats.get('total_records', 0)}\n"
            f"- total_violations: {stats.get('total_violations', 0)}"
        )

    @filter.command("censor_prompt_help")
    async def censor_prompt_help(self, event: AstrMessageEvent):
        """查看自定义提示词和JSON返回格式说明"""
        default_rules = self._get_config("default_review_rules", "")
        custom_rules = self._get_config("custom_review_rules", "")
        yield event.plain_result(
            "提示词说明:\n"
            "- 审核提示词由插件固定生成（含默认规则 + 你的自定义规则）\n"
            f"- default_review_rules: {'已配置' if default_rules.strip() else '未配置'}\n"
            f"- custom_review_rules: {'已配置' if custom_rules.strip() else '未配置'}\n"
            '- 你只需要填写"额外禁止什么"，不需要写提示词模板\n'
            "- 你不需要写 JSON 返回格式，插件会自动附加\n"
            "- LLM 必须严格返回 JSON，不要返回额外文字\n"
            f"- JSON 格式: {self.REQUIRED_JSON_FORMAT}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("censor_force_check")
    async def censor_force_check(self, event: AstrMessageEvent):
        """管理员命令：立刻执行一次当前群的 LLM 审查并按规则禁言"""
        try:
            if event.get_platform_name() != "aiocqhttp":
                yield event.plain_result("❌ 此命令仅支持 QQ 平台")
                return

            message_obj = event.message_obj
            group_id = str(message_obj.group_id) if message_obj and message_obj.group_id else ""
            if not group_id:
                yield event.plain_result("❌ 此命令仅支持群聊使用")
                return

            # 刷新该群最近事件引用
            self.latest_events[group_id] = event

            total_messages = self.message_buffer.get_total_messages(group_id)
            if total_messages == 0:
                yield event.plain_result("ℹ️ 当前群缓冲区暂无可审查消息。")
                return

            yield event.plain_result(
                f"🧪 管理员强制审查已启动，当前缓冲消息 {total_messages} 条。"
            )

            await self._process_group_messages(group_id)

            yield event.plain_result("✅ 强制审查执行完成。")
        except Exception as e:
            logger.error(f"强制审查命令执行失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 强制审查失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("test_ban")
    async def test_ban_command(self, event: AstrMessageEvent):
        """测试禁言功能 - 禁言发送者1分钟（仅管理员可用）"""
        try:
            if event.get_platform_name() != "aiocqhttp":
                yield event.plain_result("❌ 此命令仅支持 QQ 平台")
                return

            message_obj = event.message_obj
            if not message_obj.group_id:
                yield event.plain_result("❌ 此命令仅支持群聊使用")
                return

            group_id = str(message_obj.group_id)
            user_id = str(message_obj.sender.user_id)
            user_name = event.get_sender_name()

            # 验证 Event 类型
            AiocqhttpMessageEvent = self._try_get_aiocqhttp_event_class()
            if AiocqhttpMessageEvent and not isinstance(event, AiocqhttpMessageEvent):
                yield event.plain_result("❌ Event 类型不匹配")
                return

            # 执行禁言（1分钟测试）
            client = event.bot
            test_duration = 60

            logger.info(f"执行测试禁言：群 {group_id}，用户 {user_id}（{user_name}），时长 {test_duration} 秒")

            try:
                try:
                    group_id_int = int(group_id)
                    user_id_int = int(user_id)
                except ValueError as ve:
                    logger.error(f"类型转换失败 - group_id={group_id}, user_id={user_id}: {ve}")
                    yield event.plain_result(
                        f"❌ 参数错误：无法将群ID或用户ID转换为整数\n"
                        f"group_id={group_id}, user_id={user_id}"
                    )
                    return

                ret = await client.api.call_action(
                    'set_group_ban',
                    group_id=group_id_int,
                    user_id=user_id_int,
                    duration=test_duration
                )

                if ret is None or (isinstance(ret, dict) and ret.get('retcode') == 0):
                    logger.info(f"测试禁言成功：用户 {user_id}")
                    yield event.plain_result(
                        f"✅ 测试成功！用户 {user_name}（{user_id}）已被禁言 {test_duration} 秒。\n"
                        f"这是一次测试，用于验证禁言功能是否正常工作。"
                    )
                else:
                    error_msg = ret.get('message', '未知错误') if isinstance(ret, dict) else f"未知返回: {ret}"
                    logger.error(f"测试禁言失败: {ret}")
                    yield event.plain_result(
                        f"❌ 禁言失败：{error_msg}\n"
                        f"可能原因：Bot 不是管理员、权限不足、或 API 调用失败。"
                    )

            except Exception as e:
                logger.error(f"调用禁言 API 失败: {e}", exc_info=True)
                yield event.plain_result(
                    f"❌ API 调用异常：{str(e)}\n"
                    f"请检查 Bot 配置和权限。"
                )

        except Exception as e:
            logger.error(f"测试禁言命令执行失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 命令执行失败：{str(e)}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听所有群消息"""
        try:
            # 检查是否为 QQ 平台
            if event.get_platform_name() != "aiocqhttp":
                return

            # 获取消息信息
            message_obj = event.message_obj
            group_id = str(message_obj.group_id) if message_obj.group_id else None
            user_id = str(message_obj.sender.user_id) if message_obj.sender else None
            self_id = str(message_obj.self_id) if getattr(message_obj, "self_id", None) else None
            message_str = event.message_str
            timestamp = message_obj.timestamp
            user_name = event.get_sender_name()

            if not group_id or not user_id or not message_str.strip():
                return

            # 保存最新的 event 对象
            self.latest_events[group_id] = event

            # 不缓冲机器人自身消息
            if self_id and user_id == self_id:
                return

            # 初始化检测时间
            self.message_buffer.ensure_check_time_initialized(group_id)

            # 白名单检查
            whitelist_users = self._get_config("whitelist_users", [])
            if user_id in [str(u) for u in whitelist_users]:
                return

            # 群组过滤
            enabled_groups = self._get_config("enabled_groups", [])
            if enabled_groups and group_id not in [str(g) for g in enabled_groups]:
                return

            # 添加消息到缓冲区
            self.message_buffer.append_message(group_id, user_id, {
                "message": message_str,
                "timestamp": timestamp,
                "user_name": user_name
            })

            current_count = self.message_buffer.get_total_messages(group_id)
            batch_size = self._get_config("batch_size", 10)
            trigger_mode = self._get_config("trigger_mode", "hybrid")

            logger.info(
                f"群 {group_id} 消息累积: {current_count}/{batch_size}（mode={trigger_mode}）"
            )

            # 检查是否需要触发检测
            if await self._should_trigger_check(group_id):
                await self._process_group_messages(group_id)

        except Exception as e:
            logger.error(f"处理群消息时出错: {e}", exc_info=True)

    async def _should_trigger_check(self, group_id: str) -> bool:
        """判断是否应该触发检测"""
        trigger_mode = self._get_config("trigger_mode", "hybrid")
        total_messages = self.message_buffer.get_total_messages(group_id)
        check_interval = self._get_config("check_interval", 60)
        batch_size = self._get_config("batch_size", 10)

        last_check = self.message_buffer.get_check_time(group_id)
        current_time = time.time()
        time_elapsed = current_time - last_check

        if trigger_mode == "time_only":
            return False
        elif trigger_mode == "count_only":
            if total_messages < batch_size:
                logger.info(f"count_only 未触发：群 {group_id} 当前 {total_messages}/{batch_size}")
            return total_messages >= batch_size
        elif trigger_mode == "hybrid":
            time_triggered = time_elapsed >= check_interval
            count_triggered = total_messages >= batch_size
            return time_triggered or count_triggered
        elif trigger_mode == "strict_hybrid":
            time_triggered = time_elapsed >= check_interval
            count_triggered = total_messages >= batch_size
            if not (time_triggered and count_triggered):
                logger.info(
                    f"strict_hybrid 未触发：群 {group_id} time_ok={time_triggered}, count={total_messages}/{batch_size}"
                )
            return time_triggered and count_triggered

        return False

    async def _periodic_check(self):
        """定时检测任务（用于包含时间条件的模式）"""
        while True:
            try:
                check_interval = self._get_config("check_interval", 60)
                await asyncio.sleep(check_interval)

                logger.debug("执行定时检测...")

                # 遍历所有群组
                for group_id in list(self.message_buffer.buffer.keys()):
                    total_messages = self.message_buffer.get_total_messages(group_id)

                    # 如果有消息，则进行检测
                    if total_messages > 0:
                        trigger_mode = self._get_config("trigger_mode", "hybrid")
                        last_check = self.message_buffer.get_check_time(group_id)
                        time_elapsed = time.time() - last_check

                        if trigger_mode == "time_only":
                            if time_elapsed >= check_interval:
                                logger.info(f"群 {group_id} 定时触发检测（消息数: {total_messages}）")
                                await self._process_group_messages(group_id)
                        else:
                            if await self._should_trigger_check(group_id):
                                logger.info(f"群 {group_id} 定时轮询触发检测（消息数: {total_messages}）")
                                await self._process_group_messages(group_id)

                # 清理过期消息（超过 1 小时）
                self.message_buffer.cleanup_old_messages()

                # 清理过期违规记录
                if self.violation_manager:
                    await self.violation_manager.cleanup_expired_records()

                # 清理已清空的群组的event缓存（防止内存泄漏）
                self._cleanup_stale_events()

            except asyncio.CancelledError:
                logger.info("定时检测任务被取消")
                break
            except Exception as e:
                logger.error(f"定时检测出错: {e}", exc_info=True)

    def _cleanup_stale_events(self):
        """清理不再活跃的群组的event缓存"""
        active_groups = set(self.message_buffer.buffer.keys())
        stale_groups = [gid for gid in self.latest_events.keys() if gid not in active_groups]

        for group_id in stale_groups:
            del self.latest_events[group_id]

        if stale_groups:
            logger.debug(f"清理了 {len(stale_groups)} 个不活跃群组的event缓存")

    async def _process_group_messages(self, group_id: str):
        """处理群组的累积消息（修复竞态条件和防止消息丢失）"""
        lock = self.message_buffer.get_or_create_lock(group_id)

        # 第1阶段：在锁保护下，创建消息快照（不清除）
        async with lock:
            try:
                # 创建快照但不清空缓冲区（防止LLM失败时消息丢失）
                messages_dict = dict(self.message_buffer.buffer.get(group_id, {}))

                if not messages_dict:
                    return

                total_count = sum(len(msgs) for msgs in messages_dict.values())
                logger.info(f"群 {group_id} 获取消息快照（{total_count} 条），即将进行 LLM 分析...")

                # 更新检测时间
                self.message_buffer.update_check_time(group_id)
            except Exception as e:
                logger.error(f"缓冲区快照获取失败: {e}", exc_info=True)
                return

        # 第2阶段：释放锁后进行耗时的LLM分析
        try:
            llm_provider = self._get_config("llm_provider", "")
            if not llm_provider:
                logger.warning(f"群 {group_id} 未配置 LLM 提供商，跳过分析")
                return

            default_rules = self._get_config("default_review_rules", "")
            custom_rules = self._get_config("custom_review_rules", "")

            violations = await self.llm_analyzer.analyze_messages(
                group_id, messages_dict, llm_provider, default_rules, custom_rules
            )

            # 第3阶段：LLM分析成功后，在锁保护下清除已处理消息
            async with lock:
                try:
                    # 应用recent_message_limit限制
                    recent_message_limit = self._get_config("recent_message_limit", 0)
                    if recent_message_limit > 0:
                        self.message_buffer.trim_recent_messages(group_id, recent_message_limit)

                    # LLM分析成功，清空该批消息
                    self.message_buffer.snapshot_and_clear(group_id)
                except Exception as e:
                    logger.error(f"清空缓冲区失败: {e}", exc_info=True)

            if violations:
                logger.info(f"检测到 {len(violations)} 个违规用户")

                # 对每个违规用户执行禁言
                for violation in violations:
                    user_id = violation.get("user_id")
                    reason = violation.get("reason", "违规内容")

                    if not user_id:
                        continue

                    # 应用防误杀护栏
                    if not self.ban_executor.validate_and_should_ban(
                        group_id, user_id, messages_dict, reason
                    ):
                        continue

                    # 检查重复违规冷却
                    if self.violation_manager and self.violation_manager.check_repeated_violation(
                        group_id, user_id
                    ):
                        continue

                    # 执行禁言
                    event = self.latest_events.get(group_id)
                    if event and await self.ban_executor.ban_user(event, group_id, user_id, reason):
                        # 记录违规（仅在禁言成功时）
                        if self.violation_manager:
                            self.violation_manager.record_violation(group_id, user_id)
                            await self.violation_manager.save_records()
            else:
                logger.info(f"群 {group_id} 未检测到违规内容")

        except Exception as e:
            logger.error(f"LLM分析或后续处理失败: {e}，消息保留在缓冲区供重试", exc_info=True)

    async def terminate(self):
        """插件卸载时取消定时任务并保存数据"""
        if self.timer_task:
            self.timer_task.cancel()
            try:
                await self.timer_task
            except asyncio.CancelledError:
                pass
            logger.info("定时检测任务已停止")

        # 保存所有违规记录到持久化存储
        if self.violation_manager:
            try:
                await self.violation_manager.save_records()
                logger.info("已保存所有违规记录到磁盘")
            except Exception as e:
                logger.error(f"保存违规记录失败: {e}", exc_info=True)

        logger.info("群聊消息审核插件已卸载")
