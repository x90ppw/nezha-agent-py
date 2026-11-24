# nezha-agent-py

用 Python 写的哪吒（Nezha）探针 agent，零依赖（只用 Python 标准库），支持两种运行模式。是 [nezha-agent-js](https://github.com/x90ppw/nezha-agent-js) 的 Python 对应版，协议实现与配置方式保持一致。

很多免费托管面板会查杀/禁止官方 `nezha-agent` 二进制进程。本项目就是为这种环境准备的：**纯 Python 模式下根本不存在 agent 二进制**——探针逻辑就是你的 Python 应用本身。只要面板能跑 Python（3.8+），`python3 main.py` 即可，不需要 pip 安装任何东西。

## 两种运行模式

### 1. 纯 Python 模式（`NEZHA_MODE=pure`，默认，核心模式）

- **零二进制依赖**：不下载任何可执行文件、不落地 agent 程序、不拉起 agent 子进程。
- **协议是真实现，不是模拟**：Python 标准库没有 HTTP/2 客户端，本项目在原始 socket 上手写了最小 HTTP/2 客户端（`nezha/http2.py`：帧、流、流控、PING/GOAWAY 处理）和 HPACK 编解码（`nezha/hpack.py`：含 RFC 7541 附录 B 的 Huffman 解码，已通过 RFC 附录 C 全部示例向量测试），再手写 gRPC 帧（5 字节头：1 字节压缩标志 + 4 字节大端长度）和最小化 protobuf 编解码（`nezha/protobuf.py`），按官方 proto 定义的真实字段号与哪吒面板通信。字段号来源：官方仓库 [nezhahq/agent](https://github.com/nezhahq/agent) 的 `proto/nezha.proto`，参考副本附在本仓库 `nezha/nezha.proto`。
- **隐蔽性**：进程列表里只有你自己的 `python3` 进程；磁盘上只有普通的 `.py` 文件。
- 鉴权与官方 agent 一致：每条 RPC 带 metadata `client-secret` / `client-uuid`（同时发送下划线写法 `client_secret` / `client_uuid`，与官方 AuthHandler 行为相同）。

已实现的 RPC（服务 `proto.NezhaService`）：

| RPC | 状态 | 说明 |
|---|---|---|
| `ReportSystemInfo2` (Host → Uint64Receipt) | ✅ | 注册/上报主机信息，新版面板主路径 |
| `ReportSystemInfo` (Host → Receipt) | ✅ | 老版面板回退（Info2 返回 UNIMPLEMENTED 时自动降级） |
| `ReportSystemState` (stream State → stream Receipt) | ✅ | 默认每 2 秒上报一次状态 |
| `RequestTask` (stream TaskResult ⇄ stream Task) | ✅ | 见下方任务表 |
| `ReportGeoIP` (GeoIP → GeoIP) | ✅ | 经 ipapi.co 查询本机公网 IP/国家后上报，失败静默跳过 |
| `IOStream` (终端) | ❌ | 未实现 |

任务（Task）处理情况（类型号来自官方 `model/task.go`）：

| 类型 | 名称 | 状态 |
|---|---|---|
| 1 | HTTPGet | ✅ 返回状态码与耗时 |
| 2 | ICMPPing | ⚠️ 返回失败结果：标准库无法开原始套接字，无法真实测 ICMP（不伪造数据） |
| 3 | TCPPing | ✅ TCP 连接耗时 |
| 4 | Command | ✅ 执行并回传输出；`NEZHA_DISABLE_COMMAND=1` 可禁用 |
| 5 | Terminal | ❌ 返回 `successful=false` |
| 6 | Upgrade | ❌ 返回 `successful=false`（纯 Python 模式没有可升级的二进制） |
| 7 | Keepalive | ✅ 直接确认 |
| 10 | ReportHostInfo（已废弃） | ✅ 确认，主机信息由 10 分钟定时上报兜底 |
| 8/9/11–20 | TerminalGRPC / NAT / FM / ReportConfig / ApplyConfig / ServerTransferApply / Exec / Fs* | ❌ 返回 `successful=false` |

上报的数据（Host / State）：平台、系统版本、CPU 型号、内存/磁盘总量与用量、开机时间、架构、CPU 使用率、负载、上下行流量总量与速率、TCP/UDP 连接数、进程数。其中网卡流量、swap、连接数、进程数、CPU 时间依赖 Linux `/proc`，非 Linux 平台这些项报 0；温度与 GPU 未采集。

### 2. 二进制模式（`NEZHA_MODE=binary`）

跑官方 agent，但做伪装和守护：

- 从官方 GitHub Releases 下载对应平台的 agent：
  `https://github.com/nezhahq/agent/releases/latest/download/nezha-agent_<os>_<arch>.zip`
  （如 `nezha-agent_linux_amd64.zip`；os ∈ linux/darwin/windows，arch ∈ amd64/arm64/arm/386）。解包用标准库 `zipfile`。
- 落地改名（存在 `.runtime/` 目录，名字会持久化、不每次随机）：
  - Linux：`.music-player` / `.spotify-client` / `.netease-music` 随机选一
  - Windows：`MusicPlayer_<6位随机十六进制>.exe`
- **参数走配置文件**：生成 `.runtime/config.yml`（权限 0600），用 `nezha-agent -c config.yml` 启动——命令行里只有伪装名和 `-c`，`ps` 看不到服务器地址和密钥。
- 守护：进程退出后指数退避（2s 起、上限 60s）自动重启；收到 SIGINT/SIGTERM 时一并带走子进程。

## 快速开始

```bash
# 1. 准备配置（任选其一）
export NEZHA_SERVER=nezha.example.com:8008   # 面板 gRPC 地址，host:port
export NEZHA_KEY=<面板里的客户端密钥>          # 不要写进代码/仓库

# 或在同目录建 .env 文件，内容同上（一行一个 KEY=VALUE）

# 2. 启动（纯 Python 模式）
python3 main.py

# 二进制模式
NEZHA_MODE=binary python3 main.py
```

在只能跑一个 Python 应用的面板上，把本仓库文件并入你的应用目录，在应用入口先设好环境变量再导入 `nezha.agent.run_pure` 即可（或单独起一个进程跑 `main.py`）。

## 配置表

全部走环境变量或同目录 `.env`（环境变量优先）：

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `NEZHA_SERVER` | ✅ | — | 面板地址 `host:port`（可带 `https://` 前缀）。无端口时默认 8008 |
| `NEZHA_KEY` | ✅ | — | 面板客户端密钥（client secret）。只放环境变量/.env，绝不入库 |
| `NEZHA_MODE` | — | `pure` | `pure` 纯 Python 模式 / `binary` 官方二进制模式 |
| `NEZHA_UUID` | — | 自动 | agent 的 UUID（须为标准 UUID 格式）。不填则首次生成并存到 `.nezha-uuid` 文件（已 gitignore），重启保持不变 |
| `NEZHA_TLS` | — | 自动 | 是否 TLS：端口 443 或 `https://` 前缀时自动开；可显式设 `1`/`0` |
| `NEZHA_INSECURE_TLS` | — | 关 | `1` 时跳过 TLS 证书校验（自签面板才需要） |
| `NEZHA_DISABLE_COMMAND` | — | 关 | `1` 时拒绝执行面板下发的 Command 任务 |
| `NEZHA_REPORT_INTERVAL` | — | `2` | 状态上报间隔（秒） |

## 目录结构

```
main.py             入口：加载配置、按模式分发
config.py           环境变量/.env 解析、UUID 持久化
binary.py           二进制模式：下载/解包/伪装命名/配置传参/守护
nezha/
  nezha.proto       官方 proto 参考副本（来自 nezhahq/agent）
  protobuf.py       最小 protobuf 编解码（varint/fixed/length-delimited）
  hpack.py          HPACK 编解码（静态表、动态表、Huffman 解码）
  huffman_table.py  RFC 7541 附录 B 的 Huffman 码表（由 RFC 原文生成并逐项校验）
  http2.py          基于原始 socket 的最小 HTTP/2 客户端（帧、流、流控）
  grpc.py           gRPC 帧与客户端（unary、双向流）
  messages.py       Host/State/Task/TaskResult/GeoIP 的编解码（按真实字段号）
  hostinfo.py       主机与状态采样（os/sys，/proc 计数）
  tasks.py          面板任务处理（HTTP/TCP 探测、命令、保活）
  agent.py          纯 Python 模式主循环：注册、状态流、任务流、GeoIP、重连
tests/
  test_codecs.py    编解码单元测试（含 RFC 7541 附录 C 的 HPACK 向量）
  fake_dashboard.js 本地联调用的假面板（Node http2 服务端）
  run_integration.py 本地联调：对假面板跑完整会话并断言结果
```

## 测试

```bash
python3 -m unittest discover -s tests -v     # 编解码单元测试
python3 tests/run_integration.py             # 联调（需要 node >= 18）
```

## 说明与边界

- 这是**最小可用实现**：覆盖注册、状态上报和常用探测任务；终端（IOStream/Terminal）、NAT、文件管理（Fs*）、远程配置（ApplyConfig）等高级功能未实现，面板下发这类任务时会收到明确的失败结果，不会挂起。
- 与官方 agent 的差异：主机信息每 10 分钟定时重报（官方是 IP 变化时重报）；GeoIP 只在连接建立与重报时查询。
- 安全：仓库里不含任何真实密钥/令牌；`.env`、`.nezha-uuid`、`.runtime/` 都已写入 `.gitignore`。
- 需要 Python ≥ 3.8，无第三方依赖。
