# RAD Debugger MCP

独立的 Windows x64 原生调试 MCP 服务。通过 RAD Debugger 进行启动、附加、断点、单步、调用栈和表达式检查。

**不需要 Unreal Editor、UnrealEditor MCP、UE 插件、PythonScriptPlugin 或正在运行的编辑器。** 普通带 PDB 的 C/C++ EXE、UnrealEditor、游戏客户端和 Dedicated Server 都使用同一个原生调试流程。UE 类型格式化由 RAD 自身提供，兼容性需要在目标引擎版本验证。

当前实现使用 MCP stdio。每个调试会话拥有一个无图形 RAD 后端、随机回环端口、独立认证令牌和配置目录。认证令牌不通过 MCP 返回，也不写入配置文件。不暴露原始 RAD 命令、shell 或内存写入工具；`rad_launch` 会执行指定的本地 EXE，应只用于明确授权的目标。

## 构建与启动

要求：Windows x64、Python 3.11+、Git、MSVC x64 Build Tools 与 Windows SDK。当前验证环境为 Python 3.14、MCP Python SDK 1.26.0、MSVC 19.38。

先克隆本项目和官方 RAD 源码，再构建独立后端：

```powershell
git clone https://github.com/cc8887/raddebugger-mcp.git
cd raddebugger-mcp
git clone https://github.com/EpicGames/raddebugger.git ../raddebugger
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-backend.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1
```

已经存在源码目录时跳过相应 clone。`build-backend.ps1` 默认从相邻的本地仓库创建 `.build/raddebugger` 构建副本，并固定在 `771f63dd35d3d17f0c7914c5031959282802be5f`；不会改动原始 RAD 仓库。可用 `-Upstream ../your-rad-checkout` 指定其他源码位置。构建后的来源与 SHA-256 写入 `.build/backend-manifest.json`。

`start.ps1` 会设置模块路径，因此开发阶段也可以使用已有 MCP SDK 的 Python 启动，不要求 editable install。stdio 启动后没有命令提示符；它等待 MCP 客户端。

支持 MCP stdio 的客户端配置示例（安装包后；将 `<absolute-repo-path>` 替换成实际检出目录的绝对路径）：

```json
{
  "mcpServers": {
    "raddebugger": {
      "command": "<absolute-repo-path>/.venv/Scripts/python.exe",
      "args": ["-m", "raddebugger_mcp"]
    }
  }
}
```

也可以将 `command` 指向 `powershell.exe`，`args` 设置为 `-NoProfile -ExecutionPolicy Bypass -File <absolute-repo-path>/scripts/start.ps1` 的各个独立参数。其他位置的后端使用 `--backend <exe>` 或 `RAD_MCP_BACKEND`。会话目录使用 `--session-root <directory>`。

## 调试流程

1. `rad_create_session` 得到 `session_id`。这一步仅启动 RAD 后端。
2. `rad_set_breakpoint` 设置函数、行或地址断点；可以在目标启动前设置。
3. `rad_launch` 启动 EXE，默认停在入口；或通过 `rad_list_processes` 找到明确的 PID，再 `rad_attach`。
4. `rad_continue` 返回 `wait_after_run_gen` 和 `wait_after_stop_count`；传给 `rad_wait_for_stop` 等待新的停点或退出。
5. 在停点调用 `rad_get_stack`、`rad_get_state`、`rad_evaluate`；用 `rad_select_context` 选择线程或栈帧。
6. `rad_step` 支持源码 into/over/out 及指令 into/over；`rad_run_to_line` 运行到行。
7. `rad_close_session` 默认分离并恢复应用。只有显式 `terminate_debuggee=true` 才终止目标。

例如启动 UE 项目，参数仍然是普通 EXE 参数：

```json
{
  "session_id": "<rad_create_session 返回的 ID>",
  "program": "<absolute-engine-path>/Engine/Binaries/Win64/UnrealEditor.exe",
  "args": ["<absolute-project-path>/MyGame.uproject", "-log"],
  "stop_at_entry": true
}
```

这里没有向编辑器发送 MCP 请求。现有 Editor、客户端或 Server 也可以独立附加。目标必须允许原生调试，并有与二进制匹配的符号；首次 PDB → RDI 转换可能耗时。`debug_subprocesses` 默认关闭，可在 launch 中明确开启。

## 工具

| 工具 | 功能 |
| --- | --- |
| `rad_create_session`, `rad_list_sessions`, `rad_close_session` | 会话生命周期 |
| `rad_list_processes`, `rad_launch`, `rad_attach` | 选择、启动、附加目标 |
| `rad_get_state`, `rad_wait_for_stop` | 状态、停点代际与有界等待 |
| `rad_set_breakpoint`, `rad_list_breakpoints`, `rad_remove_breakpoint` | 行、函数、地址断点 |
| `rad_continue`, `rad_pause`, `rad_step`, `rad_run_to_line` | 原生执行控制 |
| `rad_get_stack`, `rad_select_context` | 调用栈、线程及栈帧选择 |
| `rad_evaluate`, `rad_address_to_line` | 类型/值与地址到源码映射 |
| `rad_open_crash_dump` | 本地 dump 装载；受 dump/PDB 数据覆盖限制 |

## 返回值的含义

- `requested`：命令已发送，应使用返回的基线等待停点；不代表已经停住。
- `stopped`：观察到了比基线更新的停点。
- `exited`：观察到目标进程集合已清空；具体退出码需要结合输出/停点原因确认。
- `timeout`：截止时间前没有新的停点，**不会自动终止、暂停或重放命令**。
- 断点的 `resolution=configured` 只表示 RAD 配置已存在；不能据此宣称机器地址已解析或断点已命中。
- 表达式返回 `ok`、`messages`、`read_failed`、`run_gen`、`stop_count`、线程/帧、时间戳和读取方式。补丁后端的 `memory_freshness=direct_process_read` 表示进程内存绕过异步页面缓存；读取不完整或失败会令 `ok=false`。寄存器、栈展开和符号仍由 RAD 提供，`consistency=same_stop_epoch` 不代表整个进程的原子快照。显示值目前上限 512 字符。
- 使用先前快照作决策时，传 `expected_run_gen`/`expected_stop_count`。过期快照返回 `STALE_SNAPSHOT`；运行中的值检查返回 `TARGET_RUNNING`。

会话状态、目标参数、stdout/stderr 和 RAD 日志保存在本地会话目录，不会自动上传。普通关闭与 MCP stdin EOF 会尝试先分离目标，再退出后端；后端还禁用了 Windows 的 debugger-exit-kills-debuggee 行为。断电、强制结束主机、目标自身异常等不属于正常清理验收。

## 验证

```powershell
$env:PYTHONPATH = "$PWD/src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-fixture.ps1
.\.venv\Scripts\python.exe -u scripts/smoke.py
```

smoke 测试使用官方 Python MCP SDK，通过真实 stdio 协议调用本服务，再经认证 TCP 控制 RAD，调试项目内自带的 MSVC/PDB 测试程序。结果写入 `artifacts/smoke-*/result.json`。在禁止本机 socket/Windows 异步 I/O 的沙箱中，需要在主机权限环境运行测试。

详细能力边界与验收记录见 [docs/verification.md](docs/verification.md)。运行时只依赖 RAD 和 MCP SDK；这些测试不需要 UE 安装。

真实 UE 项目验收（可选，需要已有项目、UE 5.8.1 和匹配插件 DLL/PDB）：

```powershell
.\.venv\Scripts\python.exe -u scripts/smoke_ue.py --project ../RmlUiUnrealTest/RmlUiUnrealTest.uproject --engine "<path-to-UE_5.8>"
```

该脚本通过 MCP 启动现有项目内的 `RmlUi.Animation.MovieSceneRuntime.Lifecycle` 自动化，验证函数/行断点、源码单步、局部结构体与完成计数；分离后再附加同一个正在运行的 UE PID，验证引擎 Tick 地址断点、指令单步和帧计数。测试使用 NullRHI，不作为画面验收；不安装插件、不编译或改写目标源码，也不调用 UnrealEditor MCP。结束时仅清理本脚本启动并保留了原始句柄的 UE 进程。结果位于 `artifacts/ue-smoke-*/result.json`。外部 UE 工程和原始私有验收产物不随本仓库发布，普通单元测试与 C/PDB smoke 不需要它们。

## 源码与维护

- `src/raddebugger_mcp/`：MCP 工具、会话、分帧与 Metadesk 回复解析。
- `native/upstream.patch`：固定提交的少量桥接改动；新增代码在 `native/mcp_extension.c`。
- `scripts/build-backend.ps1`：独立源码副本、补丁和后端构建。
- `tests/`、`scripts/smoke.py`：协议回归与真实调试验收。
- `scripts/smoke_ue.py`、`scripts/ue_pe.py`：可选现有 UE 项目验收及引擎导出地址解析，均不属于服务运行时依赖。

RAD 补丁提供：回环绑定、每会话认证、TCP 请求重组、正常断连处理、较大的回复队列、无图形链接修复、进程/线程数值选择、调用栈/配置证据、独立目标配置输入，以及直接内存求值和读取失败报告。启动时不覆盖 RAD 自动保存的活动项目文件，已有断点 ID 保持有效。升级上游时需要重新核对补丁和协议，不能直接替换为任意官方 release EXE。

当前没有 Blueprint VM、Actor/Component 枚举、资产编辑、PIE 操作、GPU 调试、Android/主机调试、远程调试、内存写入或目标函数调用 API。`rad_evaluate` 使用 RAD 表达式语言，不承诺任意 C++ 表达式兼容性。Shipping/优化构建可能缺失局部变量。

表达式示例：`g_counter` 读取变量；`hex(cast(uint64)(&g_counter))` 获取地址；`*(cast(int32*)0xADDRESS)` 读取明确地址。优先使用 RAD 的 `cast(type)`，一元运算的操作数再加一层括号；内置整数类型为 `int32`/`uint64` 等。不要把 `(uint64)&value` 当作可靠的 C++ 转换写法。求值失败时以 `ok=false` 和 `messages` 为准，不使用显示出的占位数值。

项目采用 MIT 许可；RAD Debugger 的版权与 MIT 声明保存在 [native/UPSTREAM_LICENSE](native/UPSTREAM_LICENSE)。MCP 使用 [官方 Python SDK](https://py.sdk.modelcontextprotocol.io/v1/)。
