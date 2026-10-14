# DSH 子代理桥 · astrbot_plugin_dsh_bridge

把任务交给本机的 **DeepSeek Harness（DSH）** 去跑，再把结果收回聊天。
子代理在指定工作区里干活：读写文件、跑命令、调用它自己装的那些插件（比如几个生图插件）。

跟"传声筒"的区别：这个插件还会让我（AstrBot 这边的模型）当**操作手**——
先想怎么做、派提示词、自己回看它干了什么、不满意再派一轮，验收过了才给你结果。

```
你 ──「帮我做 X」──► 我（洛水，AstrBot 侧）
                       │ 判断要不要动用 Harness、写第一轮提示词
                       │ dsh_task（串行队列 + 超时 + 白名单）
                       ▼
              cmd /c dsh --profile headless --json "<任务>"   （cwd = 工作区）
                       │
                       ▼
              DSH headless 子代理：读文件 / 跑 shell / 调自己的插件 / 生图
                       │ stdout：按行 JSON 事件（session·status·thinking·tool_call·tool_result·text·final）
                       │ 同时在 ~/.dsh/sessions/<工作区键>/<会话>/ 落会话记录
                       ▼
        我自查：dsh_trace 读会话记录，看它调了什么、结果如何
                       │ 不满意 → 带同一个 session_id 再派一轮
                       ▼
              验收通过，把结果 + 验证方式汇报给你
```

---

## 1. 安装

把 `astrbot_plugin_dsh_bridge` 整个目录放进 AstrBot 的 `data/plugins/`，然后在 WebUI 插件管理里重载。

依赖：本机有 `dsh` 命令（全局 `npm i -g @deepseek-ai/dsh`，或者用本插件的 `/dsh装dsh` 装便携版）。
它自己那侧需要的插件（生图等）装在哪个 profile，就在配置里把 `profile` 指向哪个。

## 2. 快速上手

1. 装插件 → WebUI 重载
2. `/dsh体检` —— 它会告诉你这台机器缺什么（Node / dsh / profile / key / 工作区权限 / 便携 DSH）
3. `/dsh 列出工作区里的文件` —— 跑通就说明链路好了
4. 要让**我**进入操作手回路：`/操作手 开`

## 3. 命令一览（人用）

| 命令 | 作用 |
|---|---|
| `/dsh 任务` | 派一条任务给子代理，跑完把结果发回来 |
| `/dsh续 session-xxxx 任务` | 接着某次 DSH 会话继续（多轮打磨） |
| `/dsh会话` | 列当前工作区里最近的会话，挑一个继续或回看 |
| `/dsh过程 [会话id]` | 看它那轮到底调了什么工具、命令、结果如何 |
| `/操作手 开｜关｜状态` | 切换操作手模式（见第 5 节） |
| `/dsh网页` | 拿 `dsh web` 的地址（没开就自动起一个），浏览器里可视化看会话 |
| `/dsh停web` | 关掉本插件起过的 web 进程 |
| `/dsh装dsh` | 一键装便携 DSH（不用全局安装） |
| `/dsh体检` | 环境自检，输出"缺什么、怎么补" |
| `/dsh状态` | 开关、队列忙不忙、profile、工作区、超时、白名单 |

## 4. 模型侧工具（我调用的）

| 工具 | 作用 |
|---|---|
| `dsh_task(task, timeout_seconds, session_id, continue_last)` | 派单。`continue_last=true` 自动接上这个聊天上一次的 DSH 会话，不用手抄 id |
| `dsh_trace(session_id, limit)` | 回看会话记录：工具调用、参数、结果、它说过的话（跨帧解压会话文件） |
| `dsh_sessions(limit)` | 列最近会话 |

## 5. 操作手模式

```
/操作手 开      # 进入：我按「计划 → 派单 → 自查 → 迭代 → 汇报」来用这条通道
/操作手 关      # 退出：回到平常聊天
/操作手 状态    # 看现在开着没、谁在什么时候切的
```

- 只有开着时才给我注入操作手册（`inject_manual` 是总开关，`manual_text` 可换成你自己的手册）
- 状态存在插件数据目录 `operator.json`，重启不丢；想默认开着改 `operator_default`
- `/dsh` 系列命令不受它影响，随时能手动派活
- 切换要过白名单，别人开不了

## 6. 配置（WebUI 插件配置页）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `workspace` | 空 | 子代理的工作区。**留空 = 插件数据目录下的 `workspace/`**（换机器零配置）；建议纯英文路径，DSH 内部 shell 对中文路径会乱码 |
| `profile` | headless | 用哪个 DSH profile 跑。插件装在哪个 profile，子代理才能用到那些工具 |
| `dsh_cmd` | dsh | dsh 可执行文件。填 `dsh` 就自动找（PATH → npm 全局目录 → 便携运行时），找不到再填完整路径 |
| `dsh_install_dir` | 空 | 便携 DSH 装哪。留空 = 数据目录下 `dsh-runtime` |
| `dsh_version` | latest | 装便携 DSH 用的版本 |
| `timeout_seconds` | 300 | 单次任务上限，超时连进程树一起杀 |
| `max_final_chars` | 3000 | 回传答案的截断长度 |
| `api_key` | 空 | DSH 用的 DeepSeek key。**留空 = 自动用 AstrBot 里配好的 DeepSeek key** |
| `api_base` | 空 | 自定义端点，作为 `DEEPSEEK_BASE_URL` 传给子代理；留空用 DSH 默认官方端点 |
| `model` | 空 | 默认模型。留空 = DSH 自带的 `deepseek-flash`；填了会写进 profile patch 的 `agent-default-model` |
| `share_python_path` | true | 把 AstrBot 的 Python 目录前置进子代理的 PATH（否则它在沙箱里找不到 python/node） |
| `auto_fix_acl` | true | 撞上沙箱 `grantWrite` 被拒时自动 icacls 授权并重跑 |
| `allow_user_ids` | 空 | 白名单 QQ，逗号分隔；留空表示不限制（自己用时填上更安全） |
| `log_runs` | true | 每次运行落一行到数据目录 `runs.jsonl` |
| `inject_manual` | true | 是否允许注入操作手册（还要操作手模式开着才生效） |
| `manual_text` | 空 | 自定义操作手册文本 |
| `operator_default` | false | 插件加载时操作手模式默认开着吗 |
| `web_auto_start` | true | `/dsh网页` 时若 web 没跑就自动起 |
| `web_port` | 3939 | `dsh web` 端口 |
| `web_workspace` | 空 | `dsh web` 的工作目录，留空 = 用上面的 `workspace`（两边一致，会话才能在界面里互相看见） |

## 7. 在 WebUI 里看得见会话

DSH 的会话按「工作区」存：`~/.dsh/sessions/<工作区键>/session-xxxx/`，记录里不带 profile。
只要 `dsh web` 的工作目录和插件的 `workspace` 是同一个，`/dsh` 跑出来的会话就会出现在 WebUI 的会话列表里，
可以打开、接着手改、看过程和产物。

工作区键的换算法（`~` 后面是大写十六进制的 Unicode 码点）：
`D:\dsh_work` → `--D-dsh_work--`；含中文的路径例如 `D:\dsh工作区` → `--D-dsh~5DE5~4F5C~533A--`

## 8. 护栏

- **串行队列**：同一时刻只跑一个任务，避免 profile / 会话存储并发互踩
- **超时即杀进程树**：不留半死的 DSH 进程
- **工作区固定**：子代理跑不出这个圈（它自己的沙箱再叠一层）
- **白名单 + 总开关**：随时一键关掉，切模式也要过白名单
- **全程留痕**：每次运行写 `runs.jsonl`（任务、耗时、会话、用到的工具、错误）；会话记录还能事后回看

## 9. 换一台电脑怎么用

理想情况就三步：装插件 → 重载 → `/dsh体检`。

- **没有 DSH**：`/dsh装dsh` 在数据目录里装一份便携版，装完自动认，不用全局安装
- **key 不用填**：留空就取 AstrBot 里配好的 DeepSeek key。DSH 的凭据优先级里"启动环境"最高，
  所以以环境变量 `DEEPSEEK_API_KEY` 喂给它立刻生效，不用改 DSH 任何配置
- **默认模型就是 flash**：DSH 自带 `deepseek-official / deepseek-flash`
- **沙箱授权自动修**：Windows 上没权限改 ACL 时自动 `icacls` 授权后重跑
- **工作区留空最省事**：落在插件数据目录下，换机器不用改路径

## 10. 已知坑与对策

这些都是踩过并修掉的，换环境时可能再遇到：

| 现象 | 原因 | 对策 |
|---|---|---|
| 面板/命令报 `cannot import name 'xxx' from 'dsh_runner'` | AstrBot 重载只清插件自己的模块，顶层名模块留在 `sys.modules` 里 | 插件用 `importlib` 按文件路径 + 独占模块名重新加载运行器 |
| 子代理说"环境缺失，跑不了" | DSH 沙箱里 PATH 没有 python/node（Windows 还有商店别名坑） | `share_python_path` 前置 AstrBot 的 Python 目录 |
| 回看会话是空的 | 会话文件是逐次追加的**多帧 zstd**，单帧解只能拿到开头那行 | `stream_reader(..., read_across_frames=True)` |
| 报 `SetNamedSecurityInfoW failed (Win32 5): grantWrite(...)` | 当前用户对工作区没有改 ACL 的权限 | 自动 `icacls /grant <用户>:(OI)(CI)F /T /C` 后重跑 |
| 插件市场有个同名插件（作者 Fantasality） | 撞名，功能不同（它扫端口连 DSH 网页服务） | 认准本目录，别按市场的仓库信息"更新" |

## 11. 还没做

- **实时进度回传**：现在是"派单 → 等 → 回结果"，事件流已经解析出来了，加个回调就能边跑边报
- **常驻会话（ACP）**：一次进程内多轮，省掉每次冷启动的几秒（见 `dsh夺舍可行性` 文档的方案 B）
- **任务档案自动化**：目前靠操作手册约定我在工作区里建任务目录留痕

## 12. 文件与数据落在哪

```
data/plugins/astrbot_plugin_dsh_bridge/
├── main.py            AstrBot 侧：工具、命令、操作手模式、手册注入、会话回看
├── dsh_runner.py      纯运行器（不依赖 AstrBot，可单测）：spawn、事件解析、超时、ACL、体检、便携安装
├── _conf_schema.json  配置 schema
├── metadata.yaml      插件元数据
└── README.md          这份文档

data/plugin_data/astrbot_plugin_dsh_bridge/
├── runs.jsonl         每次运行的日志
├── sessions.json      这个聊天上一次用的 DSH 会话（多轮续接用）
├── operator.json      操作手模式开关状态
├── dsh_web.json       本插件起过的 dsh web 进程信息（pid + 带令牌的地址）
└── workspace/         workspace 留空时的默认工作区
```

> 各版本的改动看同目录下的 `CHANGELOG.md`。
