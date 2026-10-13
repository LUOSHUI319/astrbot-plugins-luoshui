# DSH 子代理桥 v0.2.0 更新说明

**发布日期**：2026-10-13
**适用版本**：AstrBot >= 4.14.0
**上一版本**：v0.1.0（内部版，未发布）

---

## 一句话

第一次进仓库：把本机的 DeepSeek Harness 变成机器人的手和脚，而且机器人不是传声筒，是操作手。

---

## 这一版有什么

### 派单与取回

- 以 `dsh --profile headless --json "<任务>"` 的形式把任务交给子代理，工作目录固定在配置的工作区
- 解析子代理按行输出的 JSON 事件（`session` / `status` / `thinking` / `tool_call` / `tool_result` / `text` / `final`），
  回传「最终答复 + 它动手做了哪几步 + token 用量 + 会话 id」
- 子代理在它自己那个 DSH 里能用的工具，就是它 profile 里装的那些（读写、跑命令、生图等）

### 操作手回路（这版的重点）

只做派单和转达等于传声筒，所以补了三件事：

| 能力 | 实现 |
| --- | --- |
| 过程可回看 | `dsh_trace` 直接读 `~/.dsh/sessions/<工作区键>/<会话>/session.v*.jsonl.zstd`，把工具调用、参数、结果、它说过的话排出来 |
| 多轮不手抄 id | `dsh_task(continue_last=true)` 自动接上这个聊天上一次的 DSH 会话；会话 id 记在插件数据目录 `sessions.json` |
| 手册注入 | `on_llm_request` 往系统提示里注入操作手册，让模型按 计划 → 派单 → 自查 → 迭代 → 汇报 来用这条通道 |

操作手模式用指令临时开关：`/操作手 开` / `/操作手 关` / `/操作手 状态`。关掉就是普通聊天，手册不注入。

### 命令行

| 指令 | 作用 |
| --- | --- |
| `/dsh 任务` | 派一条任务 |
| `/dsh续 session-xxxx 任务` | 接着某次会话继续 |
| `/dsh会话` | 列最近会话 |
| `/dsh过程 [会话id]` | 看那一轮的过程 |
| `/操作手 开｜关｜状态` | 切操作手模式 |
| `/dsh网页` / `/dsh停web` | 起/关 `dsh web`，浏览器里可视化看会话 |
| `/dsh装dsh` | 一键装便携 DSH |
| `/dsh体检` / `/dsh状态` | 环境自检 / 运行状态 |

### 装到别人机器上也能用

- **dsh 自动定位**：配置路径 → PATH → npm 全局目录 → 便携运行时，找不到会告诉你
- **key 不用手填**：`api_key` 留空就取 AstrBot 里配好的那个 DeepSeek key，以 `DEEPSEEK_API_KEY` 环境变量喂给子代理
  （DSH 的凭据优先级里「启动环境」最高，覆盖存储和账号，所以立刻生效，不用改 DSH 任何配置）
- **默认模型就是 flash**：用 DSH 自带的 `deepseek-official / deepseek-flash`
- **沙箱授权自动修**：撞上 `grantWrite` 被拒时自动 `icacls` 授权并重跑
- **工作区留空即零配置**：落在插件数据目录下的 `workspace/`
- `/dsh体检` 把 Node、npm、pnpm、dsh 版本、DSH home、凭据、profile、工作区权限一次报清楚

---

## 踩过并修掉的坑

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| 重载后报 `cannot import name 'xxx' from 'dsh_runner'` | AstrBot 重载只清插件自己的模块，顶层名模块留在 `sys.modules` 里，一直是旧的 | 用 `importlib` 按文件路径 + 独占模块名重新加载运行器 |
| 子代理说「环境缺失，跑不了」 | DSH 沙箱里 PATH 没有 python/node（Windows 还有应用商店别名坑） | `share_python_path` 把 AstrBot 的解释器目录前置进子代理 PATH |
| 回看会话是空的 | 会话文件是逐次 flush 追加的**多帧 zstd**，单帧解只能拿到开头那行 | `stream_reader(..., read_across_frames=True)` |
| 报 `SetNamedSecurityInfoW failed (Win32 5): grantWrite(...)` | 当前用户对工作区没有改 ACL 的权限 | 自动 `icacls /grant <用户>:(OI)(CI)F /T /C` 后重跑一次 |

---

## 已知限制

- 还是「派单 → 等 → 回结果」，没有实时进度回传（事件流已经解析出来了，加回调就能做）
- 每次都是新起一个 headless 进程，冷启动会有几秒开销；常驻会话（ACP）留待后续
- 子代理能做多少事，取决于你在它那个 profile 里装了什么插件
