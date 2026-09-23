# dev-macos: 本机开发环境附件

> **仅本机开发，服务器部署不装。**

## 为什么需要

macOS Docker Desktop 把容器跑在一个 Linux 虚拟机里，虚拟机看不到宿主机的
VPN 网卡。当模型网关在 VPN 内网时，容器无法直连。

执行器通过 `TALOS_HOST_FORWARD` 环境变量把内网主机名映射到 Docker 的
`host-gateway`（宿主机 IP），容器因此连接宿主机的对应端口。本中转脚本
在宿主机上监听该端口，把流量转发到 VPN 内网的真实地址。

## 安装

```bash
# 1. 确认 ~/.hermes/talos.env 里有 TALOS_HOST_FORWARD
#    格式: hostname:host.docker.internal:port

# 2. 安装 launchd 服务（自动读取 talos.env 中的 TALOS_HOST_FORWARD）
mkdir -p ~/.hermes/talos/logs
cp deploy/dev-macos/com.talos.dev-port-relay.plist ~/Library/LaunchAgents/
# 编辑 plist，把 $TALOS_HOST_FORWARD 替换为 talos.env 里的实际值
# 把 $HOME 替换为实际路径
launchctl load ~/Library/LaunchAgents/com.talos.dev-port-relay.plist
```

## 确认存活

```bash
# 替换 <port> 为 TALOS_HOST_FORWARD 里的端口号
nc -z 127.0.0.1 <port> && echo "relay alive" || echo "relay down"
```

或者：

```bash
lsof -i :<port> | head -3
```

launchd 配置了 `KeepAlive`，进程退出后 3 秒内自动拉起。
