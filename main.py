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
        self.latest_event_timestamps: Dict[str, float] = {}

        # 保护 latest_events 字典的锁
        self._events_dict_lock = asyncio.Lock()

        # 群组处理状态跟踪（防止重复处理）
        # 状态机：IDLE -> PROCESSING -> IDLE
        # 结构: {group_id: {"status": "IDLE"|"PROCESSING", "lock": asyncio.Lock}}
        self.group_processing_states: Dict[str, str] = {}
        self._processing_states_lock = asyncio.Lock()

        # 重试队列：用于存储失败需要重试的群组信息
        # 结构：[(group_id, messages_dict, retry_count, timestamp), ...]
        self.retry_queue: List[tuple] = []
        self._retry_queue_lock = asyncio.Lock()

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

    async def _touch_latest_event(self, group_id: str, event: AstrMessageEvent):
        """更新群组最近事件引用及时间戳"""
        async with self._events_dict_lock:
            self.latest_events[group_id] = event
            self.latest_event_timestamps[group_id] = time.time()

    async def _get_latest_event(self, group_id: str) -> Optional[AstrMessageEvent]:
        """安全地获取群组的最新event"""
        async with self._events_dict_lock:
            return self.latest_events.get(group_id)

    async def _try_acquire_processing_lock(self, group_id: str) -> bool:
        """尝试获取群组的处理锁（防止重复处理）

        返回 True 表示成功获得锁，群组进入 PROCESSING 状态
        返回 False 表示群组已在处理中，需要跳过
        """
        async with self._processing_states_lock:
            if self.group_processing_states.get(group_id) == "PROCESSING":
                logger.warning(f"群 {group_id} 正在处理中，跳过本轮检测")
                return False

            self.group_processing_states[group_id] = "PROCESSING"
            return True

    async def _release_processing_lock(self, group_id: str):
        """释放群组的处理锁，恢复到 IDLE 状态"""
        async with self._processing_states_lock:
            self.group_processing_states[group_id] = "IDLE"

    async def _enqueue_retry(self, group_id: str, messages_dict: Dict, retry_count: int = 0):
        """将失败的消息加入重试队列

        Args:
            group_id: 群组 ID
            messages_dict: 消息字典
            retry_count: 已重试次数
        """
        max_retries = 3
        if retry_count >= max_retries:
            logger.warning(f"群 {group_id} 已达到最大重试次数({max_retries})，放弃处理")
            return

        async with self._retry_queue_lock:
            self.retry_queue.append((group_id, messages_dict, retry_count, time.time()))
            logger.info(f"群 {group_id} 消息已加入重试队列（重试次数: {retry_count}/3）")

    async def _process_retry_queue(self):
        """处理重试队列中的消息

        用于定时任务中，处理之前失败需要重试的消息
        """
        if not self.retry_queue:
            return

        async with self._retry_queue_lock:
            items_to_process = self.retry_queue.copy()
            self.retry_queue.clear()

        logger.info(f"开始处理重试队列（{len(items_to_process)}项）")

        for group_id, messages_dict, retry_count, enqueue_time in items_to_process:
            try:
                # 增加重试计数
                new_retry_count = retry_count + 1
                logger.info(f"重试群 {group_id} 的消息分析（重试次数: {new_retry_count}/3）")

                # 重新处理这个群组的消息
                # 为了避免重复进入PROCESSING，我们不再设置处理状态，直接处理
                lock = await self.message_buffer.get_or_create_lock(group_id)
                trigger_mode = self._get_config("trigger_mode", "hybrid")
                recent_limit = int(self._get_config("recent_message_limit", 50)) if trigger_mode == "strict_hybrid" else 0

                # 执行LLM分析
                try:
                    llm_provider = self._get_config("llm_provider", "")
                    if not llm_provider:
                        logger.warning(f"群 {group_id} 未配置 LLM 提供商，放弃重试")
                        continue

                    default_rules = self._get_config("default_review_rules", "")
                    custom_rules = self._get_config("custom_review_rules", "")
                    llm_api_timeout = float(self._get_config("llm_api_timeout", 30))

                    violations, error_code, should_retry = await self.llm_analyzer.analyze_messages(
                        group_id, messages_dict, llm_provider, default_rules, custom_rules,
                        llm_api_timeout=llm_api_timeout
                    )

                    # 重新检查重试条件
                    if error_code != "success":
                        if should_retry and new_retry_count < 3:
                            # 继续重试
                            await self._enqueue_retry(group_id, messages_dict, new_retry_count)
                        else:
                            # 放弃重试
                            logger.error(f"群 {group_id} 重试失败: error_code={error_code}，放弃处理")
                        continue

                    # 成功：执行禁言逻辑
                    if violations:
                        logger.info(f"重试成功：群 {group_id} 检测到 {len(violations)} 个违规用户")

                        for violation in violations:
                            user_id = violation.get("user_id")
                            reason = violation.get("reason", "违规内容")

                            if not user_id or not self.ban_executor.validate_and_should_ban(user_id, messages_dict, reason):
                                continue

                            if self.violation_manager and await self.violation_manager.check_repeated_violation_async(group_id, user_id):
                                continue

                            event = await self._get_latest_event(group_id)
                            if event and await self.ban_executor.ban_user(event, group_id, user_id, reason):
                                if self.violation_manager:
                                    await self.violation_manager.record_violation_async(group_id, user_id)

                        if self.violation_manager:
                            await self.violation_manager.save_records()
                    else:
                        logger.info(f"重试成功：群 {group_id} 无违规内容")

                except Exception as e:
                    logger.error(f"重试群 {group_id} 的分析失败: {e}", exc_info=True)
                    if new_retry_count < 3:
                        await self._enqueue_retry(group_id, messages_dict, new_retry_count)

            except Exception as e:
                logger.error(f"处理重试队列项失败: {e}", exc_info=True)

    async def initialize(self):
        """插件初始化：加载配置、启动定时任务"""
        trigger_mode = self._get_config("trigger_mode", "hybrid")
        batch_size = self._get_config("batch_size", 10)
        llm_provider = self._get_config("llm_provider", "")

        # 初始化违规管理器并加载历史记录
        violation_cooldown_seconds = self._get_config("violation_cooldown_seconds", 3600)
        self.violation_manager = ViolationManager(self.data_dir, violation_cooldown_seconds)
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

        stats = await self.violation_manager.get_stats_async() if self.violation_manager else {}

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
            await self._touch_latest_event(group_id, event)

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
                group_id_int = int(group_id)
                user_id_int = int(user_id)
            except ValueError as ve:
                logger.error(f"类型转换失败 - group_id={group_id}, user_id={user_id}: {ve}")
                yield event.plain_result(
                    f"❌ 参数错误：无法将群ID或用户ID转换为整数\n"
                    f"group_id={group_id}, user_id={user_id}"
                )
                return

            api_timeout_seconds = float(self._get_config("api_timeout_seconds", 60))
            try:
                ret = await asyncio.wait_for(
                    client.api.call_action(
                        'set_group_ban',
                        group_id=group_id_int,
                        user_id=user_id_int,
                        duration=test_duration
                    ),
                    timeout=api_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"测试禁言 API 调用超时（>{api_timeout_seconds}s）：群 {group_id}，用户 {user_id}"
                )
                yield event.plain_result(
                    f"❌ API 调用超时（>{api_timeout_seconds}s）\n"
                    f"请检查 Bot 连接状态或平台响应。"
                )
                return
            except TypeError as te:
                logger.error(f"调用禁言 API 参数错误: {te}", exc_info=True)
                yield event.plain_result(
                    f"❌ API 参数错误：{te}\n"
                    f"请检查 Bot 配置和参数类型。"
                )
                return
            except Exception as e:
                logger.error(f"调用禁言 API 失败: {e}", exc_info=True)
                yield event.plain_result(
                    f"❌ API 调用异常：{str(e)}\n"
                    f"请检查 Bot 配置和权限。"
                )
                return

            if self.ban_executor.is_ban_api_success(ret):
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
            await self._touch_latest_event(group_id, event)

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

            lock = await self.message_buffer.get_or_create_lock(group_id)
            async with lock:
                # 添加消息到缓冲区（锁保护）
                self.message_buffer.append_message(group_id, user_id, {
                    "message": message_str,
                    "timestamp": timestamp,
                    "user_name": user_name
                })

                trigger_mode = self._get_config("trigger_mode", "hybrid")
                if trigger_mode == "strict_hybrid":
                    recent_limit = int(self._get_config("recent_message_limit", 50))
                    self.message_buffer.trim_recent_messages(group_id, recent_limit)

                current_count = self.message_buffer.get_total_messages(group_id)

            batch_size = self._get_config("batch_size", 10)
            trigger_mode = self._get_config("trigger_mode", "hybrid")

            logger.info(
                f"群 {group_id} 消息累积: {current_count}/{batch_size}（mode={trigger_mode}）"
            )

            # 检查是否需要触发检测
            if await self._should_trigger_check(group_id, current_count):
                await self._process_group_messages(group_id)

        except Exception as e:
            logger.error(f"处理群消息时出错: {e}", exc_info=True)

    async def _should_trigger_check(self, group_id: str, total_messages: Optional[int] = None) -> bool:
        """判断是否应该触发检测（原子操作：检查+决策在同一把锁内）"""
        trigger_mode = self._get_config("trigger_mode", "hybrid")
        check_interval = self._get_config("check_interval", 60)
        batch_size = self._get_config("batch_size", 10)

        # 原子性保证：获取计数和判断触发条件在同一把锁内
        lock = await self.message_buffer.get_or_create_lock(group_id)
        async with lock:
            if total_messages is None:
                total_messages = self.message_buffer.get_total_messages(group_id)

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

                # 首先处理重试队列（如果有）
                if self.retry_queue:
                    logger.debug(f"开始处理{len(self.retry_queue)}条重试消息")
                    await self._process_retry_queue()

                # 遍历所有群组，筛选本轮需要检测的群
                groups_to_process: List[str] = []
                for group_id in self.message_buffer.get_group_ids_snapshot():
                    lock = await self.message_buffer.get_or_create_lock(group_id)
                    async with lock:
                        total_messages = self.message_buffer.get_total_messages(group_id)

                    # 如果有消息，则进行检测
                    if total_messages > 0:
                        trigger_mode = self._get_config("trigger_mode", "hybrid")
                        last_check = self.message_buffer.get_check_time(group_id)
                        time_elapsed = time.time() - last_check

                        if trigger_mode == "time_only":
                            if time_elapsed >= check_interval:
                                logger.info(f"群 {group_id} 定时触发检测（消息数: {total_messages}）")
                                groups_to_process.append(group_id)
                        else:
                            if await self._should_trigger_check(group_id, total_messages):
                                logger.info(f"群 {group_id} 定时轮询触发检测（消息数: {total_messages}）")
                                groups_to_process.append(group_id)

                # 并发处理，避免单个群处理慢导致全局队头阻塞
                if groups_to_process:
                    results = await asyncio.gather(
                        *(self._process_group_messages(group_id) for group_id in groups_to_process),
                        return_exceptions=True,
                    )
                    for group_id, result in zip(groups_to_process, results):
                        if isinstance(result, Exception):
                            logger.error(f"群 {group_id} 定时并发检测失败: {result}", exc_info=True)

                # 清理过期消息
                message_buffer_max_age = int(self._get_config("message_buffer_max_age", 3600))
                self.message_buffer.cleanup_old_messages(max_age_seconds=message_buffer_max_age)

                # 清理过期违规记录
                if self.violation_manager:
                    violation_records_expire_days = int(self._get_config("violation_records_expire_days", 7))
                    await self.violation_manager.cleanup_expired_records(max_age_days=violation_records_expire_days)

                # 清理已清空的群组的event缓存（防止内存泄漏）
                await self._cleanup_stale_events()

            except asyncio.CancelledError:
                logger.info("定时检测任务被取消")
                break
            except Exception as e:
                logger.error(f"定时检测出错: {e}", exc_info=True)

    async def _cleanup_stale_events(self):
        """清理不再活跃的群组的event缓存"""
        active_groups = {
            group_id
            for group_id in self.message_buffer.get_group_ids_snapshot()
            if self.message_buffer.get_total_messages(group_id) > 0
        }
        current_time = time.time()
        max_event_age_seconds = self._get_config("event_cache_max_age", 1800)

        async with self._events_dict_lock:
            stale_groups = [
                gid for gid in list(self.latest_events.keys())
                if (gid not in active_groups)
                or (current_time - self.latest_event_timestamps.get(gid, 0) > max_event_age_seconds)
            ]

            for group_id in stale_groups:
                del self.latest_events[group_id]
                self.latest_event_timestamps.pop(group_id, None)

        if stale_groups:
            logger.debug(f"清理了 {len(stale_groups)} 个不活跃群组的event缓存")

    async def _process_group_messages(self, group_id: str):
        """处理群组的累积消息（修复竞态条件和防止消息丢失）"""
        # 第0阶段：尝试获取处理锁，防止重复处理
        if not await self._try_acquire_processing_lock(group_id):
            return

        try:
            lock = await self.message_buffer.get_or_create_lock(group_id)
            trigger_mode = self._get_config("trigger_mode", "hybrid")
            recent_limit = int(self._get_config("recent_message_limit", 50)) if trigger_mode == "strict_hybrid" else 0

            # 在锁保护下原子执行：深拷贝快照 + 立即清空缓冲区
            async with lock:
                try:
                    messages_dict = self.message_buffer.snapshot_and_clear(group_id)

                    if not messages_dict:
                        return

                    total_count = sum(len(msgs) for msgs in messages_dict.values())
                    logger.info(f"群 {group_id} 获取消息快照（{total_count} 条），即将进行 LLM 分析...")
                except Exception as e:
                    logger.error(f"缓冲区快照获取失败: {e}", exc_info=True)
                    return

            # 第2阶段：释放锁后进行耗时的LLM分析；若失败则回灌快照，避免消息丢失
            try:
                llm_provider = self._get_config("llm_provider", "")
                if not llm_provider:
                    logger.warning(f"群 {group_id} 未配置 LLM 提供商，跳过分析")
                    async with lock:
                        self.message_buffer.restore_snapshot(group_id, messages_dict, recent_limit)
                    return

                default_rules = self._get_config("default_review_rules", "")
                custom_rules = self._get_config("custom_review_rules", "")
                llm_api_timeout = float(self._get_config("llm_api_timeout", 30))

                violations, error_code, should_retry = await self.llm_analyzer.analyze_messages(
                    group_id, messages_dict, llm_provider, default_rules, custom_rules,
                    llm_api_timeout=llm_api_timeout
                )

                # 处理 LLM 异常（P0 修复：所有失败都回灌，避免消息丢失）
                if error_code != "success":
                    logger.warning(f"LLM 分析返回错误: error_code={error_code}, should_retry={should_retry}")

                    # CRITICAL: 无论是否重试，都必须回灌消息
                    async with lock:
                        self.message_buffer.restore_snapshot(group_id, messages_dict, recent_limit)

                    if should_retry:
                        # 网络临时错误：加入重试队列
                        await self._enqueue_retry(group_id, messages_dict, retry_count=0)
                        logger.info(f"群 {group_id} 消息已回灌并加入重试队列（网络错误，将重试）")
                    else:
                        # 非临时错误也要回灌，防止消息永久丢失
                        logger.warning(f"群 {group_id} 分析失败且不重试，消息已回灌到缓冲区")

                self.message_buffer.update_check_time(group_id)

            except Exception as e:
                async with lock:
                    self.message_buffer.restore_snapshot(group_id, messages_dict, recent_limit)
                logger.error(f"LLM分析失败，已回灌消息快照: {e}", exc_info=True)
                return

            try:
                records_updated = False

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
                            user_id, messages_dict, reason
                        ):
                            continue

                        # 检查重复违规冷却
                        if self.violation_manager and await self.violation_manager.check_repeated_violation_async(group_id, user_id):
                            continue

                        # 执行禁言
                        event = await self._get_latest_event(group_id)
                        if event and await self.ban_executor.ban_user(event, group_id, user_id, reason):
                            # 记录违规（仅在禁言成功时）
                            if self.violation_manager:
                                await self.violation_manager.record_violation_async(group_id, user_id)
                                records_updated = True

                    if records_updated and self.violation_manager:
                        await self.violation_manager.save_records()
                else:
                    logger.info(f"群 {group_id} 未检测到违规内容")

            except Exception as e:
                logger.error(f"违规处置流程失败: {e}", exc_info=True)

        finally:
            # 无论成功失败，都要释放处理锁
            await self._release_processing_lock(group_id)

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
