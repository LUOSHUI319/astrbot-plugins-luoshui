# 用户卡片

查询 QQ 用户资料，渲染成一张资料卡图片。

通过 OneBot 接口拉取昵称、性别、年龄、QQ 等级、注册时间、群等级、入群时间等字段，
再结合本地补充库和可选的第三方接口补齐生日、星座、生肖、现居、职业、VIP 等，
用 Pillow 渲染成图片发出来。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/用户卡片 @某人` | 查询被 @ 的人 |
| `/用户卡片`（同时回复某条消息） | 查询被回复消息的发送者 |
| `/用户卡片 12345678` | 直接发 QQ 号查询 |
| `/卡片设置` | 管理本地补充的资料（生日、现居、职业之类） |
| `/卡片诊断` | 排查接口取不到字段的问题 |

## 配置

| 分组 | 配置项 | 默认 | 说明 |
| --- | --- | --- | --- |
| basic | `enable` | `true` | 总开关，关掉后指令和新人自动发卡都停 |
| basic | `auto_card_on_join` | `true` | 有人入群时自动发一张该成员的资料卡 |
| basic | `cache_seconds` | `600` | 同一个人的查询缓存时间，避免频繁调接口被风控，0 表示不缓存 |
| basic | `extra_api` | 空 | 可选第三方资料接口，GET 请求、返回 JSON，字段名会自动识别 |
| basic | `extra_api_token` | 空 | 第三方接口密钥，以 `Authorization: Bearer` 形式带上 |
| permissions | `allow_other_query` | `true` | 关掉后普通成员只能查自己，管理员不受限 |
| permissions | `blocked_groups` | 空 | 逗号或空格分隔，这些群里不响应指令也不自动发卡 |
| style | 卡片样式相关 | — | 配色、字体、显示哪些字段等 |

## 依赖

- Pillow
- `fonts/` 里自带了 `box.ttf`、`ZCOOLKuaiLe-Regular.ttf`、`NotoColorEmoji.ttf`，不用另外装字体

## 支持平台

`aiocqhttp`（OneBot / NapCat 等），AstrBot `>=4.16`。
