# astrbot_plugin_quote_probability

按概率给机器人回复加上引用（Reply），替代 AstrBot 全局的「回复时引用原消息」无条件引用。

## 必要前提

先把 AstrBot 全局配置里的 **platform_settings → 回复时引用原消息（reply_with_quote）关掉**，
否则框架会在插件钩子之后无条件再引用一次，概率控制会被覆盖。
插件启动时检测到它仍然是开着的，会在日志里打一条提醒。

## 默认行为

- 引用概率 0.1（平均十条回复引用一条）
- 只在 `aiocqhttp`（OneBot / NapCat 等）生效
- 只对 LLM 回复生效，私聊和群聊都开
- 链上已经有引用、没有触发消息 id 时自动跳过
- 语音 / 图片打头的回复，引用会插到它们后面，避免 QQ 端显示错位

## 配置项

quote_settings

| 项 | 默认 | 说明 |
| --- | --- | --- |
| enabled | true | 插件总开关 |
| probability | 0.1 | 引用概率，0~1 |
| cooldown_seconds | 0 | 距上次引用不足这个秒数就跳过，0 不限制 |
| only_llm_result | true | 只对 LLM 回复生效 |
| min_text_chars | 0 | 正文短于这个字数不引用 |

scope_settings

| 项 | 默认 | 说明 |
| --- | --- | --- |
| enable_private | true | 私聊生效 |
| enable_group | true | 群聊生效 |
| platforms | ["aiocqhttp"] | 生效平台，留空为所有平台 |

debug_settings

| 项 | 默认 | 说明 |
| --- | --- | --- |
| debug_log | false | 输出命中日志 |

## 实现要点

- 钩子 `@filter.on_decorating_result(priority=-5000)`，排在私聊插件清理类钩子（-1000 及更小）之后，
  避免刚插上的引用被去引用、去重逻辑摘掉。
- 触发消息 id 取自 `event.message_obj.message_id`。
- 流式输出时该钩子只在流式结束的最终结果上跑一次（AstrBot 在增量阶段会跳过装饰阶段）。