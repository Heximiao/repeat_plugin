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
    _last_repeated: str | None = None

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

        if self._is_self(message):
            recent = list(history)[-(trigger - 1):] if len(history) >= trigger - 1 else []
            if len(history) >= trigger - 1 and all(e == text for e in recent) and text == self._last_repeated:
                self._last_repeated = None
            history.append(text)
            return

        if len(history) >= trigger - 1:
            recent = list(history)[-(trigger - 1):]
            if all(e == text for e in recent):
                if random.random() <= cfg.skip_probability:
                    if cfg.debug_mode:
                        self.ctx.logger.info("[repeat] 命中跳过概率，不复读")
                    history.append(text)
                    return

                if random.random() <= cfg.repeat_probability and text != self._last_repeated:
                    self._last_repeated = text
                    await self.ctx.send.text(text, stream_id)
                    if cfg.debug_mode:
                        self.ctx.logger.info("[repeat] 已复读: %s", text)

        history.append(text)


def create_plugin() -> RepeatPlugin:
    return RepeatPlugin()
