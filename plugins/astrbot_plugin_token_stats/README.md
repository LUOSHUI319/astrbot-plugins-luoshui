# 会话用量统计

查某个会话的 token 消耗和缓存命中率。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/token` | 本会话 今天 |
| `/token 7d` | 本会话 近 7 天 |
| `/token 30d` | 本会话 近 30 天 |
| `/token all` | 本会话 全部 |
| `/token 排行` | 所有会话 今天 的用量排行 |

别名：`用量` / `token用量` / `消耗` / `缓存命中`

## 数据来源

AstrBot 主库 `data/data_v4.db` 的 `provider_stats` 表。AstrBot 每次调用大模型都会往这张表里落一条记录：

- `umo`：会话号，比如 `default:FriendMessage:3190568934`
- `token_input_other`：没命中缓存的输入 token
- `token_input_cached`：命中缓存的输入 token
- `token_output`：输出 token
- `created_at`：记录时间，**存的是 UTC**，算本地「今天」需要按配置的时区偏移换算

命中率算法：

```
命中率 = token_input_cached / (token_input_cached + token_input_other)
```

## 注意

- 只读数据库（`mode=ro`），不写入，不会影响 AstrBot 正常记录。
- 语音、图片等不产生 token 的往来不会计入。
- 数据库路径优先取 AstrBot 自身的 `DB_PATH`，取不到才退回 `data/data_v4.db`。
