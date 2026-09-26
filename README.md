# MCP Hub（MCP 智能助手网关）

> TOS 7 Deb 单包应用 · WebUI 内嵌（iframe）· 版本 **1.0.1**

| 项 | 值 |
|---|---|
| 应用 ID | `shh13-mcp-hub` |
| 包类型 | Deb 单包（`application_type: "deb"`） |
| 打开方式 | WebUI 内嵌（`type: "iframe"`，`path: "/shh13-mcp-hub/"`） |
| 版本 | 1.0.1 |
| 分类 | `Development_Tools`, `Artificial_Intelligence` |
| 发布者 | shh |
| 开发者仓库 | <https://github.com/RyanYang163/shh13-mcp-hub> |
| 隐私政策（公网可访问） | <https://github.com/RyanYang163/shh13-mcp-hub/blob/main/PRIVACY.md> |

## 简介

一个带权限控制的 MCP 服务端，让 AI 客户端读取 TNAS 上的文件、存储与媒体元数据——默认只读。

A permission-controlled MCP server that lets an AI client read files, storage and media metadata on your TNAS - read-only by default.

## 功能

- MCP 服务端：JSON-RPC 2.0，stdio + HTTP 双传输，不引第三方 SDK
- 12 个固定命名工具，分文件 / 存储 / 媒体 / 任务四组
- 三级权限：只读（默认）/ 可写 / 危险，危险工具默认关闭
- 目录白名单 + 每次调用 resolve 校验（拒绝穿越与逃逸软链接）
- 不提供 shell_exec —— 不存在万能命令入口
- 全量审计日志 + 可复制的客户端配置片段

全部功能由 **Python 标准库 + 原生 HTML/CSS/JS** 实现，**不含任何第三方代码、第三方
图标字体或前端框架**，因此本仓库的许可证情况极为简单：全部为本项目自有代码，以 MIT 发布。

## 可选增强引擎

本应用不依赖任何外部程序，全部功能由 Python 标准库与原生前端实现。

## 权限声明

| 权限 | 用途 | 说明 |
|---|---|---|
| 网络：无宿主端口 | — | iframe 应用经平台代理与后端通信，只监听 Unix socket，不占用任何宿主端口 |
| 文件系统：`/Volume*/@apps/shh13-mcp-hub/data` | 运行期数据 | 由应用创建；是**唯一**的写入位置 |
| 文件系统：用户选择加入白名单的目录 | 读取用户文件 | **默认只读**；白名单初始为空，必须由用户显式添加 |
| 系统用户：`shh13-mcp-hub` 去连字符 | 隔离运行 | 由平台在安装时创建；**非 root** |
| 共享文件夹 | 不使用 | 不需要额外共享文件夹 |
| 容器 / 特权 | 不使用 | 本应用不是 Docker 应用，不使用任何特权能力 |

资源上限（systemd 单元内声明）：`MemoryMax=512M`、`CPUQuota=100%`、
`LimitNOFILE=65536`、`LimitNPROC=256`。

## 运行时写入路径清单（指引 12.9.6）

| 路径（除特别注明外，均相对 `/Volume*/@apps/shh13-mcp-hub/data/`） | 用途 | 格式 | 创建时机 | 增长上限 / 轮转 | 生命周期 |
|---|---|---|---|---|---|
| `config/runtime.json` | 应用运行配置（含可访问目录白名单、权限级别等） | JSON | 首次启动创建，配置变更时重写 | < 64 KB | 持久，随升级保留 |
| `db/app.db`（及同目录 `-wal` / `-shm`） | 任务队列与应用数据（SQLite） | SQLite | 首次启动建库；任务与扫描结果写入 | 已完成任务只保留最近 200 条，超出自动清理 | 持久，随升级保留；**升级绝不删库**（`PRAGMA user_version` 迁移） |
| `cache/` | 可再生的缓存（扫描索引、图片哈希等） | 二进制 / JSON | 按需生成 | 上限 2 GB，超出按 LRU 清理 | 可再生，可安全删除 |
| `tmp/` | 处理中的临时文件 | 二进制 | 任务开始时创建 | 任务结束即删；**每次启动清空全部残留** | 临时，自动清理 |
| `logs/app.log`（及 `app.log.1` … `.5`） | 应用日志（标准格式，敏感信息已脱敏） | 文本 | 服务运行时持续写入 | 单文件 2 MB，轮转保留 5 个（约 12 MB 上限） | 持久，自动轮转 |
| `output/` | 用户主动生成的结果文件 | 任意 | 用户提交导出 / 提取类任务时创建 | 由用户自行管理 | **属于用户数据，卸载不会删除** |
| `/var/api/shh13-mcp-hub.sock`（绝对路径，非 data 下） | 平台代理 Unix socket，mode `0660` | Unix socket | 服务启动时创建 | 不适用 | 每次启动前先删除残留 |
| `/var/lib/shh13-mcp-hub/`（绝对路径，systemd `StateDirectory`） | 兼容路径下的应用状态 | 目录 | systemd 创建 | 不适用 | systemd 管理 |
| `/var/log/shh13-mcp-hub/`（绝对路径，systemd `LogsDirectory`） | 兼容路径下的日志 | 目录 | systemd 创建 | 不适用 | systemd 管理 |
| `/run/shh13-mcp-hub/`（绝对路径，systemd `RuntimeDirectory`） | 运行时目录 | 目录 | systemd 创建 | 不适用 | 服务停止时由 systemd 删除 |

**本应用不写入上表以外的任何路径**，尤其**不写共享的系统 `/tmp`**——临时文件一律
走 `PrivateTmp` 提供的私有 `/tmp` 或上表中的 `data/tmp/`（指引 12.9.6）。

## 隐私政策（审核项 C3 / C4 / C5）

**公网地址（可直接访问）**：<https://github.com/RyanYang163/shh13-mcp-hub/blob/main/PRIVACY.md>

包内另有 `PRIVACY.md` 全文，`config.ini` 的 `help` 字段指向同一地址——三条路径都可查。

- **本地优先**：一个带权限控制的 MCP 服务端，让 AI 客户端读取 TNAS 上的文件、存储与媒体元数据——默认只读。所有处理都在你的 TNAS 上完成。
- **不采集**：不收集任何使用统计、遥测、设备标识或个人信息。
- **不上传**：不会把你的文件、内容或分析结果发送到任何服务器。

## 构建

```bash
./build.sh            # 默认 x86_64
./build.sh aarch64
```

产物在 `build/output/`：

```
shh13-mcp-hub_x86_64.deb
shh13-mcp-hub_x86_64.deb.sha256
```

> 包文件名**不含版本号**——版本由 GitHub Release 的 tag 承载（指引 15.1 / 4.2.4）。
> 构建器是 `tools/build_deb.py`，纯 Python 实现，不依赖 `dpkg-deb`，因此在
> Windows 开发机上也能产出真 deb 并做结构自检：
> `python3 tools/build_deb.py --inspect build/output/shh13-mcp-hub_x86_64.deb`

## 本地运行（无需 TOS 设备）

```bash
python3 src/__main__.py --tcp 127.0.0.1:18013 --data-dir ./data
# 浏览器打开 http://127.0.0.1:18013/
```

同一份业务代码，只是把 Unix socket 换成 TCP。跑测试：

```bash
python3 -m unittest discover -s tests -v
```

## 安装、升级与卸载验证

```bash
# 安装
sudo dpkg -i shh13-mcp-hub_x86_64.deb
sudo systemctl status shh13-mcp-hub
sudo journalctl -u shh13-mcp-hub -f

# socket 是否就绪（iframe 应用的关键一项）
ls -l /var/api/shh13-mcp-hub.sock
curl --unix-socket /var/api/shh13-mcp-hub.sock http://localhost/health

# 启停
sudo systemctl restart shh13-mcp-hub

# 卸载（保留数据）
sudo dpkg --remove shh13-mcp-hub
# 彻底卸载（仍按设计保留数据盘上的运行数据）
sudo dpkg --purge shh13-mcp-hub

# 升级（数据必须保留）
sudo dpkg -i shh13-mcp-hub_x86_64.deb
```

**卸载后残留说明**：`dpkg --purge` 会删除 `/usr/local/shh13-mcp-hub`、`/var/api/shh13-mcp-hub.sock`、
`/var/lib/shh13-mcp-hub`、`/var/log/shh13-mcp-hub`、systemd 单元与专用用户。
**数据盘上的 `/Volume*/@apps/shh13-mcp-hub/data/` 按设计保留**——其中 `output/` 是用户跑出来的
结果文件，属于用户数据（指引 51 / 12.9.7 要求卸载默认保留用户数据）。需要彻底清理时手动执行：

```bash
sudo rm -rf /Volume*/@apps/shh13-mcp-hub
```

## 安全设计

- **非 root 运行**：专用系统用户，`User=` / `Group=` 与 `config.ini` 的 `user` 字段一致。
- **systemd 加固**：`NoNewPrivileges`、`ProtectSystem=strict`、`ProtectHome`、
  `PrivateTmp`、`PrivateDevices`、`RestrictSUIDSGID`、`LockPersonality`、
  `RemoveIPC`、`SystemCallArchitectures=native`，可写路径用 `ReadWritePaths` 显式枚举。
- **无 shell 执行入口**：所有命令都是固定映射的具体能力，不存在
  `POST /exec` 这类接受任意命令行的接口。
- **路径白名单**：所有涉及用户文件的操作都先 `realpath` 再比对白名单根，
  目录穿越与指向白名单之外的 symlink 一律拒绝。
- **日志脱敏**：日志写入前对密码、Token、API Key、Cookie、`Bearer` 凭据做兜底打码。
- **生命周期脚本无网络操作**：`preinst` / `postinst` 只做目录、属主与服务注册，
  不含 `apt` / `pip` / `curl`。
- **不写系统目录**：不在 `/etc`、`/usr`、`/boot` 下写任何运行期配置。

## 许可证

本项目自有代码以 **MIT** 发布，全文见 [`LICENSE`](./LICENSE)。
不含任何第三方代码；第三方资源的署名与说明见 [`NOTICE`](./NOTICE) 与
[`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)。

## 真机验证状态（诚实声明）

**本应用目前只在开发机上通过了全部自动化测试与规范门禁，尚未在真机上安装验证。**

同批次的 `shh11-media-audio` 已走通完整链路（上传 → 解析 → 安装 → 启动 → 平台代理 → 前端渲染），
其余应用复用的是同一套打包器、systemd 单元模板与前端框架，但**「同结构」不等于「这个包也验过」** ——
提交前应逐个在真机上装一遍。

### 两点容易被误判的配置，都是实测结论

**1）systemd 单元里刻意没有 `PrivateTmp=true`。**
TOS 上 `/var/api` 与 `/var/log` 都是指向 `/tmp` 的软链（`/tmp` 是 tmpfs）。iframe 应用必须把
Unix socket 建在 `/var/api/<appid>.sock`，而 `PrivateTmp=true` + `ReadWritePaths=/var/api`
会让 systemd 建不出命名空间，服务直接 `226/NAMESPACE` 启动失败；即便命名空间建成，
socket 也会落在**私有** `/tmp` 里，平台 nginx 在宿主上永远看不到它 —— iframe 应用会彻底不可用。
（该项在指引 12.7 中属「Recommended」；其余加固项全部保留。）

**2）单元里必须给 `AmbientCapabilities=CAP_DAC_OVERRIDE`。**
`/var/api` 的权限是 `755 root:root`，非 root 的应用用户既不能在里建文件也不能 unlink
（实测 `touch` 与 `socket.bind()` 都是 `Permission denied`）。没有这个能力时，服务会以
`status=1/FAILURE` 反复重启、socket 永不出现。
`CapabilityBoundingSet=CAP_DAC_OVERRIDE` 把能力集从内核默认的 **41 个收窄到 1 个**，
是净减少；授予的那一个正是「在平台自有的 `/var/api` 里创建自家 socket」所必需的 ——
即指引 12.7「drop all capabilities, add only required ones」的写法。

> 附注：设备上另外几个第三方应用（含已通过审核的同批应用）正是因为第一条而以
> `226/NAMESPACE` 处于 failed 状态。
