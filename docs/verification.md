# 验收记录

日期：2026-10-24（Asia/Hong_Kong）。范围：独立 Windows x64 原生调试 MCP，版本 0.1.0。

## 实际验证

| 层级 | 结果 | 证据 |
| --- | --- | --- |
| 固定版本 RAD 后端 | MSVC release 无图形构建通过 | `.build/backend-manifest.json` |
| 协议、会话、PE 与清理单元测试 | 62/62 通过（原有 40 项 + PE 16 项 + 清理 6 项） | `python -m unittest discover -s tests -v` |
| 真实 MCP 协议 | 官方 Python SDK initialize / tools/list / tools/call 通过，20 个工具 | `artifacts/smoke-20261024-192207/result.json` |
| MSVC/PDB 调试 | 真实函数、行、地址断点命中，断点删除成功 | 同上，原生进程非 mock |
| 无预热冷会话 | 不先求值、不停入口也能命中预先配置的函数断点 | 同上，独立会话 |
| 栈与变量 | 调用栈含 `breakpoint_here` 和 `main`；局部 `local_value=12`；单步后 `g_counter=12`；切到 main 得到 `argc=1` | 同上 |
| 求值错误 | 不可读地址、除零、类型作为运算值均返回 `ok=false`，不是成功的零值 | 同上；使用 RAD 显式转换语法 |
| 状态版本 | 旧停点求值被拒绝；运行中求值被拒绝；等待超时不取消执行 | 同上及单元测试 |
| 启动/附加/暂停 | 启动、按明确 PID 附加、暂停均通过 | 同上 |
| 分离/退出 | 默认关闭与 MCP EOF 后测试程序存活，后台 RAD 无残留；显式 terminate 只对测试程序执行并生效 | 同上 |
| Dump | 用自带程序生成 Windows minidump，由独立 RAD 会话装载；按已知地址读取与暂停时的值一致；不可读地址、继续执行被拒绝；关闭成功 | 同上；不代表 dump 符号自动发现已验收 |
| 控制接口 | 仅回环监听；错误认证的 exit 命令被拒绝；真实 TCP 分片请求成功重组；分离后的目标未继承监听端口 | 同上 |
| 补丁/工作区 | 补丁可应用到干净的固定上游提交；原始 `../raddebugger` 工作区干净 | `git apply --check native/upstream.patch`；`git status --short` |

最终后端连续两轮完整集成通过：`smoke-20261024-192135` 与 `smoke-20261024-192207`。每轮 55 次 MCP 工具调用，4 次预期工具错误（过期快照、命令分隔符注入、运行中求值、dump 继续执行），另有 4 次明确的求值失败检查。日志中的 `ERROR` / `value_error` 是预期负向断言。记录包含构建 manifest，目标 stdout 还验证了正常执行和认证令牌未继承。

## 真实 UE 项目端到端验收

目标：已有外部工程 `RmlUiUnrealTest.uproject`，安装版 UE **5.8.1 / CL 56057345**，`UnrealEditor-Cmd.exe`。沿用已有 `RmlUiUnreal` 插件和匹配的 2026-10-19 DLL/PDB，不修改或重新编译目标代码，不新建“伪 UE”测试进程。

验收通过记录：`artifacts/ue-smoke-20261024-193306/result.json`；最终补充公开行断点工具和严格源码行检查后，`artifacts/ue-smoke-20261024-193406/result.json` **41 次 MCP 工具调用、零工具错误，passed=true**。启动时通过 UE 自带命令行执行单个现有测试 `RmlUi.Animation.MovieSceneRuntime.Lifecycle`；没有连接或调用 UnrealEditor MCP，UE 模块枚举也未发现 MCP 模块。

| 请求能力 | 实际证据 |
| --- | --- |
| 启动 | `rad_launch` 启动真实 UE 项目，超过 1000 个原生模块；最终记录 1059 个模块 |
| 函数断点 | `FRmlUiAnimationRuntimeLifecycleTest::RunTest` 真实命中，观察到命中计数，不只返回 configured |
| 行断点 | 使用 `rad_set_breakpoint(kind="line")` 命中 `RmlUiAnimationRuntimeTests.cpp:96`，命中计数增加 |
| 源码单步 | `rad_step(kind="over")` 从该源码第 **107 行走到 108 行**，指令地址和 stop_count 均变化 |
| 变量 | `Desc.From=0`、`To=10`、`DelaySeconds=0.25`、`DurationSeconds=0.5`、`Iterations=2`、`BindingId=7`；`CompletedCount` 从 **0 变为 1** |
| 目标运行结果 | UE 日志明确报告该 Lifecycle 自动化 `Result={Success}`，不是仅启动了进程 |
| 真实分离和恢复 | `rad_close_session` 默认分离；原进程存活、Windows 查询显示无调试器、CPU 时间继续增长 |
| 附加 | 对前一步分离后仍在运行的**同一个 PID**调用 `rad_attach`，不是另起一个 fixture，也不是仅附加等待调试器的空进程 |
| 附加后的断点/单步 | 动态解析当前 `UnrealEditor-UnrealEd.dll` 的 `UEditorEngine::Tick` 导出 RVA，地址断点精确命中，再完成指令单步 |
| UE 内存与进度 | 经 MCP 直接读取当前 Core.dll 导出的 `GFrameCounter`，最终记录 **596 → 691 → 898**，包含第一次分离后的真实推进 |
| 二次分离和收尾 | 再次确认进程存活、无调试器、CPU 推进，然后只清理保留原始句柄的自有 UE 进程；主机复查无本次 UE/RAD 后端残留 |
| 输入保护 | 记录并比较所用项目描述、测试/运行时源码、DLL/PDB 的 SHA-256，前后未变；已有脏工作未被重置 |

引擎 `UnrealEditor-Core/UnrealEd` PDB 未安装，因此引擎 Tick 验收明确属于**导出地址断点 + 指令级单步**，不宣称引擎源码单步。插件代码的源码行、局部变量和类型来自已有匹配 PDB，并已通过实际停点验证。导出 RVA 从当前 PE 文件解析并记录文件哈希，没有硬编码跨版本地址。NullRHI 仅用于减少图形开销，**不证明 UI、材质、动画画面或 GPU 调试能力**。

第一次尝试调试操作全部通过，但收尾遇到 Windows 已退出进程的句柄竞态；该记录没有被改写为通过。测试驱动现在只在原始进程句柄已变为 signaled 时接受该退出情况，仍拒绝活进程的 AccessDenied；6 项清理契约测试覆盖此区别。后续完整重跑和主机残留检查通过。

输入未变的保护范围是记录中的 **7 个文件**，不是整个工程的全树哈希。正常 UE 运行仍会更新 Saved/Intermediate、自动化临时日志和派生符号缓存。如果启动请求之后、首次捕获目标句柄之前发生通信故障，UE 可能被后端安全分离后继续运行；驱动会明确记录 `cleanup_limitation`、判定失败并要求检查，不猜测 PID 后杀进程。

本文件公开的是已执行验收的摘要与边界。上面的 `artifacts/` 路径指本地执行记录；包含机器路径、运行日志、转储和会话信息的原始产物不随源码发布。外部 UE 工程也不包含在本仓库中，重跑需要自行提供匹配目标。

## 来源

- RAD 上游：`https://github.com/EpicGames/raddebugger`
- 固定提交：`771f63dd35d3d17f0c7914c5031959282802be5f`
- RAD 版本：0.9.30 ALPHA，独立补丁构建；版本字符串中的 `dirty` 表示本项目显式应用了桥接补丁。
- Python 3.14；MCP Python SDK 1.26.0；MSVC 19.38，Windows x64。
- 后端二进制 SHA-256：`9aec30dbea3c964e949f11b5a149dcbe32bdf1d0b4f06379d0de8a98f22c1e22`。
- 验收构建使用的补丁 SHA-256：`c408ba3b80bf6159ba74e1e2b814910c20c2cf267eb7f8146452b96e860814d6`。
- 公开源码补丁 SHA-256：`d5517dca661018b8b3cf555508fe09ae5f2cd5a084d9ad6cdc77682897771c95`。发布前仅移除了补丁开头两行非补丁内容的 Git 换行警告，实际应用的源码改动未变；上述二进制仍是实际验收构建，没有重新标记旧产物。

Python MCP 服务运行时没有 UnrealEditor MCP、UE 插件或 UE Python 依赖。可选 UE 验收脚本使用既有项目插件作为被调试目标；这不构成 MCP 服务依赖。

## 修复的实际问题

1. 原始 Win32 socket 绑定全部网卡且没有会话认证，socket 还会被启动的目标继承。本版本使用回环绑定、每会话令牌和不可继承的 socket。
2. 上游把 WSARecv 的一次完成当成完整消息。本版本对每个连接的请求做有界重组，Python 客户端按帧长读取回复。
3. 无图形目标缺少 comdlg32 链接依赖，桥接构建补足该依赖。
4. IPC 的进程/线程参数原先不能根据数字解析；文件路径、`--` 后的表达式和配置 ID 也需要准确传递。本版本修复并实际验证。
5. 控制端的符号名查询原先使用非阻塞异步匹配，首次查询可暂时返回地址 0，导致放行执行后错过函数断点。本版本对每次控制端求值上下文使用共享的 5 秒符号查询截止时间；UI 保持非阻塞。模块装载时会创建新的上下文。
6. attach 命令可在 Process 实体建立前先增加 run_gen。等待器现在不会把这个过渡状态误报为“进程已退出”。
7. Dump 是后端中的离线逻辑进程，关闭时不应使用 Windows live-process detach；本版本会释放后端而不尝试原生分离该逻辑进程。
8. RAD 自动保存项目时可能覆盖 MCP 刚写入的目标配置。现在使用独立输入文件和原生配置命令，只替换目标节点，保留活动项目与断点 ID。
9. 原始求值接口可能在异步内存缓存未就绪时返回看似有效的 0。MCP 求值现在绕过进程页面缓存、清除缓存的计算值，并在读取失败时明确标记 `ok=false`。Dump 读取也要求请求区间完全被存储的内存范围覆盖，不能把部分覆盖当作完整成功。
10. 核心帧循环在引擎事件追加命令后，可能因迭代器到达末尾时被清空而再次从队列头开始，导致重复状态回复甚至重放执行命令。补丁保留最后消费的命令游标；不放宽协议解析器的重复字段检查。
11. 求值解释器可能在消息列表被复制之后才附加除零等错误；IPC 现在另外检查实际解释器状态。普通运算也拒绝将类型当作数值，避免歧义 C 风格转换被解释成按位与后悄悄返回 0。类型比较路径保持不变。

## 未证明的能力

- 已验证上述真实 UE 项目的原生 Editor-Cmd 流程；尚未验证独立打包客户端、Dedicated Server、多进程 PIE 或其他具体游戏项目。
- 没有覆盖大型 UE PDB、Live Coding、优化后的内联帧、所有 UE 类型视图或全部 dump 种类。内联帧导出按上游 inline-site 元数据解析，但本次 fixture 是 `/Od`，不作为内联调试验收。
- Dump 当前验收的是装载、按已知地址读取和生命周期；在默认系统模块栈帧下直接按测试程序的全局变量名查询未成功，不宣称完整 dump 符号调试可用。
- 本次已覆盖约 1059 个模块，但没有验证长期运行、高并发或跨机器调试。每会话命令串行，最大 8 个会话；调用栈上限 128 帧；IPC 回复有大小上限。
- 符号查询有 5 秒上限。极慢转换或匹配仍可能留下未解析的断点；`configured` 不等于 `resolved` 或 `hit`。
- `rad_evaluate` 的进程内存读取方式为 `direct_process_read`；寄存器、栈展开与元数据仍由 RAD 提供。相同停点代际不等于整个进程的原子快照，结果有明确的读取失败标记和 512 字符显示上限。
- 不覆盖 Blueprint VM、Actor/资产语义、GPU、Android、游戏主机、Linux、远程调试或 Shipping 优化变量完整性。

这些边界不会阻止独立原生调试使用，但需要在具体游戏/程序上继续验收。
