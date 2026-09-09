from collections import deque
from typing import Any, cast

import random

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件设置"
    __ui_icon__ = "settings"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置文件版本号")


class RepeatSectionConfig(PluginConfigBase):
    __ui_label__ = "复读设置"
    __ui_icon__ = "repeat"
    __ui_order__ = 1

    debug_mode: bool = Field(default=False, description="是否开启调试模式")
    trigger_count: int = Field(default=3, description="连续多少条相同消息后触发复读 (>=2)")
    repeat_probability: float = Field(default=0.8, description="复读概率 (0~1)")
    skip_probability: float = Field(default=0.1, description="完全不复读的概率 (0~1)")


class RepeatPluginConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    repeat: RepeatSectionConfig = Field(default_factory=RepeatSectionConfig)


class RepeatPlugin(MaiBotPlugin):
    config_model = RepeatPluginConfig

    _chat_history: dict[str, deque] = {}
    # 每个流(群/私聊)最近一次复读的文本。按 stream_id 隔离，
    # 避免不同群/私聊之间互相抑制或重置复读状态。
    _last_repeated: dict[str, str] = {}

    async def on_load(self) -> None:
        self.ctx.logger.info("复读插件已加载")

    async def on_unload(self) -> None:
        self.ctx.logger.info("复读插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        pass

    def _is_self(self, message: dict) -> bool:
        user_id = message.get("message_info", {}).get("user_info", {}).get("user_id", "")
        self_id = message.get("message_info", {}).get("additional_config", {}).get("self_id", "")
        return bool(user_id and self_id and user_id == self_id)

    @HookHandler(
        "chat.receive.after_process",
        name="repeat_handler",
        description="检测群聊中连续重复消息并进行复读",
        mode=HookMode.BLOCKING,
    )
    async def handle_repeat(self, **kwargs: Any) -> None:
        message = kwargs.get("message", {})
        if not isinstance(message, dict):
            return

        cfg = cast(RepeatPluginConfig, self.config).repeat
        stream_id = message.get("session_id", "")
        text = (message.get("processed_plain_text") or "").strip()

        if not stream_id or not text:
            return
        if message.get("is_notify"):
            return

        history = self._chat_history.setdefault(stream_id, deque(maxlen=10))
        trigger = max(cfg.trigger_count, 2)
        last_repeated = self._last_repeated.get(stream_id)

        # 机器人自己的消息不参与计数，避免复读消息把队列推向下一轮触发，
        # 导致同一段刷屏里 bot 反复复读（拖尾复读）。
        if self._is_self(message):
            if text == last_repeated:
                self._last_repeated.pop(stream_id, None)
            return

        if len(history) >= trigger - 1:
            recent = list(history)[-(trigger - 1):]
            if all(e == text for e in recent):
                # 同一段连续重复中已经复读过，等出现不同消息后再恢复，避免拖尾。
                if text == last_repeated:
                    history.append(text)
                    return

                if random.random() <= cfg.skip_probability:
                    if cfg.debug_mode:
                        self.ctx.logger.info("[repeat] 命中跳过概率，不复读")
                    history.append(text)
                    return

                if random.random() <= cfg.repeat_probability:
                    self._last_repeated[stream_id] = text
                    # 这一组 trigger 条消息已经消费，清空窗口，
                    # 后续相同消息重新计数，不会在长队列里反复触发。
                    history.clear()
                    await self.ctx.send.text(text, stream_id)
                    if cfg.debug_mode:
                        self.ctx.logger.info("[repeat] 已复读: %s", text)
                    return

        # 出现与最近复读不同的文本时，解除该流的复读抑制，
        # 让新的重复段可以再次触发。
        if last_repeated is not None and text != last_repeated:
            self._last_repeated.pop(stream_id, None)

        history.append(text)


def create_plugin() -> RepeatPlugin:
    return RepeatPlugin()
