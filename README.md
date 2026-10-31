# 篆香百刻 · incense-seal-agent

> **About**
>
> 古时的计时，以百刻香为界——香篆一线点燃，燃尽便是一刻光阴，主人以此知时、作息、赴约。它说的便是这样一位守在香案旁的执事：替你把记不得的事一一收好——作业的死线、账单的归期、晾在外头忘收的衣裳、漏掉的那粒药。当一缕香烧到该醒的时刻，它便细细唤你一声；你若未应，它就再燃一缕，守着这百刻时光，直到你答话。
>
> *A keeper of time, burned through the day one slow ember at a time — it remembers what you would forget, and hums a gentle warning when the seal runs out, until you reply.*

个人自用的 AI 生活助理：**事务管理 + 主动提醒 + 情绪价值**。

- **它做什么**：陪你自然聊天，同时把同一句话里的安排、记账、查证当场办妥；到点准时叫你；平时偶尔主动关心一句。
- **它是什么**：单人自用、常驻后台、单实例运行、数据全本地。唯一交互通道 = **飞书**（自建应用机器人）。
- **可用生态**：兼容 SillyTavern（酒馆）角色卡格式（PNG 图片卡 / JSON），可直接导入社区角色卡。
- **设计取向**：轻量、可读、可自持。

---

## 1. 快速开始

### 1.1 环境

- Windows（服务端）+ Mac 飞书（接收端）
- Python **3.10+**
- 语音输出需 GPU 或可用 CPU 降级；纯文本使用不装 TTS 也能跑

### 1.2 安装

```powershell
# 在仓库根目录执行；sound 相关为可选项
python -m pip install -r requirements.txt

# 语音（飞书语音消息需 lark-oapi + av；均为 wheel，零外部下载）
# requirements.txt 已含，无需单独安装

# 测试依赖
python -m pip install -e ".[dev]"
```

> 若只是想本地跑起来试，不接飞书：把 `INPUT_CHANNELS` / `OUTPUT_CHANNELS` 留空，用 `DEBUG_CLI=1` 打开 stdin 调试台即可。

### 1.3 配置

复制 [.env.example](.env.example) 为 `.env`，按需填写：

| 变量 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（必填） |
| `DEEPSEEK_BASE_URL` | 默认 `https://api.deepseek.com`（带 `/v1` 也兼容） |
| `DEEPSEEK_MODEL` | 如 `deepseek-flash` |
| `DEEPSEEK_THINKING` | `1` = 开启思考模式 |
| `DEEPSEEK_REASONING_EFFORT` | 缺省思考档；各调用点的 kind→effort 映射见 `config/default.json` 的 `model.efforts` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭据 |
| `INPUT_CHANNELS` / `OUTPUT_CHANNELS` | 收发开关，可用 `feishu` / `onebot` / `bark`（逗号分隔；留空则按凭据推导） |
| `MASTER_UID` | 默认身份标识码（`[a-z][a-z0-9_]{0,15}`，默认 `me`）；通道端点须经 `.create` / `.link` 绑定 |

可选运行开关（详见 `.env.example` 注释）：`DEBUG_CLI`、`CONNECT_RETRY_INTERVAL`、`COMPACT_HINT_TURNS`、`REPLAY_WINDOW_HOURS`、`PROACTIVE_DAILY_TARGET/CAP`、`PROACTIVE_MIN_INTERVAL`、`TTS_*`。

### 1.4 运行

```powershell
python app.py    # 或 python -m qqlife；或 启动.bat
```

- **启动强依赖飞书**：连接成功前循环提醒重试（Ctrl+C 退出），**不回退交互**。
- 连上后进入事件模式：**终端只输出、不接收指令**（收 / 发 / 状态实时打印）。用户指令唯一入口 = 接收端（消息以 `.` 或 `。` 开头）。
- 未配置任何入站通道且未开 `DEBUG_CLI=1`：直接退出（退出码 1）。
- `DEBUG_CLI=1`：stdin 调试台，以 `MASTER_UID` 直通主会话，打印含思考过程的完整输出，`.quit` 退出调试台（不影响主流程）。
- **单实例锁**：同一 App ID 不允许多实例在线（多实例会分摊飞书事件、表现酷似丢消息）。重复启动时新进程直接终止。

### 1.5 测试

```powershell
python -m pytest -q
```

用例全部离线、不读写真实 `data/`；覆盖调度解析、会话注入序、去重、身份绑定、两阶段提交等纯逻辑。

### 1.6 首次使用（建档四步）

1. **建身份**：Mac 飞书给机器人发送 `.create <标识码>`（如 `.create alice`）→ 创建身份并绑定当前端点。
2. **导入人设**：`.persona` 进入多消息导入模式，粘贴角色卡文本后 `.persona end`；或直接发送 PNG 角色卡后 `.persona card`；也可 `.persona <文本>` 单条导入。**支持酒馆（SillyTavern）角色卡**——PNG 图片卡（内嵌 V2/V3）与 JSON 均可直接导入。
3. **生成机械提示词**：`.mech`（按当前人设生成 per-uid 文案表，缺省回退全局基准）。
4. **（可选）挂载推送设备**：`.link bark <token>` → 设备收到测试推送后回 `.link ok` 完成验证。

已有身份换设备：在新端点发送 `.link <标识码>`（**不会自动建档**；不存在时提示先 `.create`）。

---

## 2. 指令一览

**指令规范**：一律英文，且以英文句号 `.` 或中文句号 `。` 开头（电脑 / 手机一致）；每类只保留一种标准形，**无别名**。
未识别的指令返回帮助，不会丢给 AI。**对话 / 能力用自然语言**。

| 指令 | 说明 |
| --- | --- |
| `.time` | 查看当前时间 |
| `.status` | 查看当前处理状态（空闲 / 处理中 + 已进行秒数） |
| `.help` | 指令帮助 |
| `.compact` | 手动压缩长期记忆：把当前对话沉淀为长期记忆（任意长度可用） |
| `.usage` | 查看 token 用量与缓存命中（上次请求 + 累计命中率） |
| `.create <标识码>` | 创建身份并绑定当前端点（**唯一建档入口**；`confirm` 用于切绑） |
| `.link <标识码>` | 绑定到**已存在**身份（`confirm` 用于改绑） |
| `.link ok` | 确认待验证端点（单向出站通道如 Bark 的连通性自检闭环） |
| `.link bark <token>` | 把单向出站设备挂到当前身份（自动发测试推送，失败即回滚） |
| `.profile` | 查看身份、人设与全部已绑定端点 |
| `.unlink` | 解绑当前端点 |
| `.persona` | 导入角色卡（兼容酒馆 SillyTavern 格式）：`.persona` 进入多消息模式 / `.persona end` 保存 / `.persona cancel` 取消 / `.persona card` 导入最近 PNG 卡 / `.persona <文本>` 单条导入 |
| `.mech` | 按当前角色卡生成机械提示词（per-uid 文案表） |
| `.purge` | 清理**本身份**的运行态数据（**保留**人设 / 机械提示 / 身份端点） |
| `.quit` | 退出调试台（仅 `DEBUG_CLI=1` 可用） |

---

## 3. 对话与能力（自然语言）

全部交给**主会话**（保留人格）；办事通过 function-calling **内联调用工具**，扮演不中断、安排不丢失。

| 你说 | 它做 |
| --- | --- |
| 任意闲聊 / 角色扮演 | 主会话按人设回复 |
| `每周一三五早上八点上物理课，上课时段别打扰` | `item` 落库（regular，sched，focus=勿扰） |
| `周六交作业，到点提醒我` | `item`（once，due）→ 到点进主会话拟稿送达 |
| `我小睡半小时，别吵我` | `item`（once，focus，start/end）——勿扰到点自动解除 |
| `午饭花了 15` / `上个月花了多少` | `expense` add / read（记账） |
| `今天 xxx 开幕了吗` | `web_search` 联网查证后作答 |
| `作业做完了` | `item` update 置 `done` 办结，此后不再提醒 |
| （发来一张图） | 多模态识别后作答（飞书图片入站） |

**主会话工具（act 不 talk，共 6 个）**：

| 工具 | action | 作用 |
| --- | --- | --- |
| `item` | add / read / update / delete | 统一事项：`regular` 给 sched / `once` 给 due；`status` active/done；`read` 默认只回进行中（`include_done=true` 看历史）；`focus=true` + start/end 即勿扰 |
| `expense` | add / read / update / delete | 记账，每笔带 id，可撤改 |
| `list_courses` | — | 列出常规事项（课程 / 例行） |
| `weather` | — | 中国境内实时天气（程序提炼成中文返回） |
| `remember_user` | — | 记用户记忆一句（进【待固化记忆】每轮注入，`.compact` 时并入【长期记忆】） |
| `web_search` | — | 联网搜索（内部子对话提炼答案，原始结果不进主会话） |

**语音输出**：模型在合适时机（重要提醒 / 道别 / 安慰 / 庆祝 / 用户要求）输出
`<voice voice="angry|sad|calm">英文口语</voice>`——标签须写在 `<output>` 块**之外、单独一行**；
文本照常发送，语音后台合成后补发（WAV → opus → 飞书语音消息）。不支持语音的通道自动降级跳过。
全程有 `[tts]` / `[语音]` / `[voice]` 日志。

**提醒显示格式**：到点提醒由模型按通知栏格式生成「用户视角标题 + 正文」，最终为 `【喝水提醒】该喝水了。`
标题模板可在 `data/<uid>/mech_prompts.json` 的 `reminder_label` 覆盖（默认 `【{title}】`）。

---

## 4. 数据与记忆

一切 per-uid 内容都在 **uid 家目录** `data/<uid>/` 下，**拷贝整个目录即完成一个人的跨设备迁移**：

```
data/<uid>/
├── persona.md          人设（.persona 导入）
├── mech_prompts.json   可选；该 uid 的机械提示词覆盖（缺省回退全局基准）
├── uid.json            身份与已绑定端点（.create / .link 维护）
├── session.json        统一记忆：facts / new / turns
├── items.json          事项（提醒 / 勿扰）
├── expenses.json       账本
├── usage.json          token 用量与缓存命中统计
└── attachments/        附件（含 TTS 临时 wav）
```

- **session.json（统一记忆）**
  - `facts`：长期记忆（`field: text`），**仅**在 `.compact` / 硬上限 fold 时更新；
  - `new`：`remember_user` 写入的待固化句，每轮随上下文注入，fold 时并入 `facts`；
  - `turns`：全部未压缩轮次，纯追加。
- **机械提示词基准**：`prompts/mech_prompts_baseline.json`（全局、不依赖任何 UID）；回退顺序 `内置 → 基准 → per-uid`。
- **`.purge` 边界**：只清**当前身份**的运行态（事项 / 账本 / 会话 / 附件）+ 该身份内存对象同步重置；**保留**人设 / 机械提示 / 身份端点。
- **`logs/chat.log`**：每次处理完整落盘（`in` / `llm_req` / `tool` / `voice` / `out`），按 10MB × 5 轮转；是排障第一现场（默认只记请求摘要，`CHAT_LOG_FULL=1` 记完整请求）。

---

## 5. 项目结构

```
incense-seal-agent/
├── app.py                  入口（薄壳：加 sys.path → qqlife.app.main）
├── 启动.bat                使用当前 Python 环境启动
├── pyproject.toml          标准 src-layout（single importable package: qqlife）
├── src/qqlife/
│   ├── app.py              应用运行时：装配 + 启动强依赖 + 事件主流程
│   ├── bootstrap.py        共享装配 + 进程生命周期（启动/关闭通知、单实例锁）
│   ├── runtime.py          per-uid 运行时注册表（store/occ/gate/scheduler/session/persona）
│   ├── commands.py         指令层（统一、少样、跨端一致）
│   ├── binding.py          身份指令族（.create / .link / .profile / .unlink）
│   ├── persona_import.py   角色卡导入（长文本 / PNG / JSON）
│   ├── mech_gen.py         .mech：按人设生成机械提示词
│   ├── session.py          会话记忆（facts / new / turns，纯数据层）
│   ├── agent_core/         事件总线 / 类型 / 日志
│   ├── agents/             主会话技能 chat、工具 mantools、记忆服务、编排
│   ├── llm/                模型档位 hub / DeepSeek 客户端 / 本地预算 budget
│   ├── platform/           通道抽象与实现（feishu / onebot / bark）、身份、出站、去重
│   ├── scheduler/          提醒调度、主动关怀 gate、勿扰状态
│   ├── task_flow/          事项与账本持久化
│   ├── tts/                语音合成后端（subprocess / service）
│   └── config/             行为配置（default.json + 轻量校验）
├── prompts/                提示词与人设模板、机械提示基准
├── tts/                    可选的本地 TTS 工作区（不随仓库分发）
├── tests/                  pytest 用例（离线）
```

语音功能需自行部署兼容服务，或在本地配置 F5-TTS 工作区、模型和参考音频；仓库不包含模型及个人音频。纯文本运行可设置 `TTS_ENABLED=0`。
