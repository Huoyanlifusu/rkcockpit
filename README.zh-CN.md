# rkcockpit

[English](README.md)

rkcockpit 是 RK 平台通用设备运维控制台。

## 运行架构

门户默认运行在作为运维上位机的 **PC** 上。文件管理器本地侧、本地命令、配置、自动发现
和审计数据都属于运行门户的机器；RK 开发板是由该 PC 通过 ADB 或 SSH 管理的远端目标。

也可以把门户运行在开发板上作为可选的 standalone 模式，但此时“本地”指该开发板，
并不是下文默认的 PC 上位机部署方式。

## 目录

```
host/      上位机后端：设备、SSH/ADB/local 传输、exec、jobs、监控、部署等
portal/    门户 HTTP 服务 + 自动发现
static/    单页前端（左侧分组导航）
deploy/    systemd 单元与安装脚本
docs/      设计与结项文档
tests/     端到端测试（stdlib unittest）
```

## 功能

面向任意可通过 SSH、ADB 或本地访问的 RK 设备的零依赖 Web 控制台：设备管理、
XFTP 式双栏文件传输、Web 终端、系统监控、进程管理、诊断、批量部署、日志中心、
密钥与分组管理、操作审计。后端纯 Python 3.9 标准库（`http.server`/`urllib`），
前端零依赖 vanilla HTML/JS。

## 界面预览

### 设备管理

![设备管理、连接信息与健康状态](docs/screenshots/device-management.png)

### 双栏文件管理

![本地与远端双栏文件管理](docs/screenshots/file-manager.png)

### RK3588 外设信息

![RK3588 摄像头、USB、I2C 等外设信息](docs/screenshots/peripherals.png)

## 智能 agent

内置 agent（OpenAI 兼容），分阶段交付：P0 对话＋只读工具、P1 流式 SSE 展示工具
调用链、P2 写工具＋审计。共 12 个受控工具——6 个只读（设备清单、连通检查、系统
信息、诊断、监控、文件列表）与 6 个写入（命令执行、命令终止、文件写入、文件操作、
部署计划、进程信号）。写入操作直接执行并自动审计留痕。在配置目录（默认 `~/.rkss`，
可用 `--conf-dir` 覆盖）下的 `llm.json` 中配置 OpenAI 兼容服务；API key 不落日志
与审计记录。

## 快速上手

```bash
# 在 PC 的仓库根目录执行。
python3 -m portal.portal
# 浏览器打开 http://127.0.0.1:8080

# 可选：注册一个本地模拟设备用于界面演示。
python3 -m portal.portal --sim
```

（可选）启用 agent：创建 `~/.rkss/llm.json`，填入 OpenAI 兼容服务的 `base_url`。

左侧导航分三组：设备（设备管理/文件管理/终端）、监控运维（监控/进程/诊断/部署/
日志中心）、管理（密钥/分组/审计），默认落在「设备管理」。

## 自动发现与导入

门户自动发现本机可通过 SSH/ADB 访问的主机：ADB 解析 `adb devices -l`；SSH 从
ARP 邻居、`~/.ssh/config`、`~/.ssh/known_hosts` 与已保存设备取候选，并发探测
TCP 22 并要求 `SSH-` banner。在「设备管理」点「自动发现」，支持全选/反选一键导入
（按 type+host 去重）；SSH 导入前可指定登录用户（默认 root），导入后自动逐台连接
检查。发现结果支持屏蔽规则（IP/CIDR/通配符/ADB SN，持久化在
`~/.rkss/discovery-rules.json`）。

## 部署：PC 上位机（Debian/Ubuntu）

```bash
sudo apt install -y openssh-client sshpass adb python3
mkdir -p ~/.rkss && chmod 700 ~/.rkss
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.rkss/auth-token
chmod 600 ~/.rkss/auth-token
python3 -m portal.portal --bind 127.0.0.1 --port 8080 \
  --auth-token-file ~/.rkss/auth-token
# 在当前 PC 安装仅回环监听的 systemd 服务：
sudo ./deploy/install.sh portal --user "$USER"
# 使用已有证书部署生产 HTTPS（不会自动申请 ACME/生成证书）：
sudo ./deploy/install.sh portal --user "$USER" --tls-nginx portal.example.com \
  /etc/ssl/portal/fullchain.pem /etc/ssl/portal/key.pem
```

后端与前端只使用 Python 3.9+ 标准库，不需要执行 `pip install`，也没有
`requirements.txt` 或 `pyproject.toml` 安装步骤。PC 仅在管理 ADB 目标时需要
`adb`，仅在使用 SSH 密码认证时需要 `sshpass`。每块被管理的开发板只需提供对应的
可达传输：已授权的 ADB 连接，或者 SSH 服务与凭据；仅作为 PC 的被管理目标时，
板上不需要安装 Python 或运行 portal。

- 密码认证需要 `sshpass`；密钥认证走 `~/.ssh` 默认密钥或托管密钥。SSH 执行使用私有
  `~/.rkss/ssh-known-hosts` 严格校验，不会自动信任首次出现的主机密钥。先通过独立可信
  渠道取得设备主机密钥的 SHA256 指纹，再显式固定发现到的密钥：

  ```bash
  ./tools/pin_ssh_host.py --host 192.0.2.10 --port 22 \
    --fingerprint SHA256:独立渠道取得的值 \
    --known-hosts-file ~/.rkss/ssh-known-hosts
  ```

  `ssh-keyscan` 只负责发现候选；只有与独立取得的指纹一致才会写入。密钥变化时会拒绝连接，
  不会静默替换。
- `adb` 首次使用需授权：`adb devices` 手动确认。
- `--bind` 默认 `127.0.0.1`，本地开发可不启用鉴权。即使配置 token，直接监听非回环
  HTTP 也会拒绝启动；只有 `--trusted-http` 能显式放行并打印醒目警告，且仅适用于已有
  VPN 等加密保护的可信网络。HTTPS 反代必须传 `--external-https`，使登录/退出 cookie
  带 `Secure` 属性。
- systemd 安装脚本在 root 控制的 `/etc/rkss-portal/` 中原子生成 `auth-token`，不会
  打印令牌。`--user USER` 可选择当前 PC 上已有的非 root 服务用户；省略时依次尝试
  `RKSS_RUN_USER`、非 root 的 `SUDO_USER`、源码目录属主，并从系统解析实际主组与家目录。
  以所选用户读取令牌后登录。浏览器会换取 8 小时 HttpOnly cookie，
  脚本可使用 `Authorization: Bearer <token>`。服务默认仅监听 `127.0.0.1:8080`；可选
  nginx 配置会校验已有证书/私钥，启用 TLS 1.2/1.3、SSE、请求大小限制、短周期 HSTS
  与安全响应头，但不会申请或生成证书。
- 升级先写入 `/opt/rkss-webui/releases`，再原子切换 `current`；`last-good` 指向上一个
  可用版本。服务重启失败会恢复旧 release 和 unit。token 与配置独立保存在
  `/etc/rkss-portal`、所选用户的 `~/.rkss`，不随 release 切换。
- Token 鉴权不会加密 HTTP 流量；8080 应保持回环监听，通过上述 HTTPS 反代、SSH
  隧道或 VPN 访问。

## HTTP API

门户提供 `/api/*` 接口：健康检查、自动发现、设备管理、文件传输、命令执行、监控/
进程/诊断/部署、传输任务、密钥/分组/审计与日志中心。完整契约见
`docs/02-host-design.md` §3（V2 API 契约，仍有效；V1 集群部分已废弃）。启用鉴权后，
仅 `/api/health`、静态登录壳与 `/api/auth/*` 公开，其余 API 均要求 session cookie
或 Bearer token。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 版本与发布

rkcockpit 当前使用 `0.x.y` 语义化版本。版本以质量和就绪状态为准，次版本目标节奏
约为 4–8 周。分支、标签、兼容性、支持范围和发布说明要求见
[版本与发布策略](docs/RELEASES.zh-CN.md)。

## 参与贡献

欢迎参与贡献！带有 `good first issue` 标签的议题很适合作为起点。对于范围较大的
改动，请先创建 Issue 以便对齐范围。详情请参阅
[CONTRIBUTING.md](CONTRIBUTING.md)。
