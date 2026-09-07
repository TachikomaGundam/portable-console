# Portable Console — 便携运维控制台

## 是什么

本机现行控制台（旧门户 + 每模型控制服务 + 采集守护，均为自写代码）的**可移植、免 root 重制版**，按插件化架构重构（设计契约见 `DESIGN.md`）。整个 bundle 就是一个目录：clone / 拷贝到任何目标设备，装好 Python 3.10+ 即可运行，**全程不需要 root**。与本机现行控制台并存，不改动 `/var/www`、`/opt`、`/etc` 的任何东西。

- 零第三方依赖：Python ≥3.10 标准库 only；前端零构建、零 CDN。
- 硬件自适应：探测不到的硬件段整段缺失，前端整卡隐藏——禁止假值/幽灵值。
- 统一入口 `console.py`：`daemon run|once` / `serve` / `card ...` / `token`。

## 快速开始

```bash
# 1. 把 bundle 放到目标设备（clone 或整目录拷贝，路径随意，不需要 root 权限目录）
git clone <repo-url> console && cd console      # 或 scp -r console/ host:~/console

# 2. 安装：生成配置 + 令牌 + 渲染 systemd 用户单元（不自动启动）
./install.sh

# 3. 启动两个服务
systemctl --user enable --now console-daemon console-server

# 4. 打开门户
#    http://127.0.0.1:8090
```

常用选项：

```bash
./install.sh --port 9090        # 覆盖配置里的 listen.port
./install.sh --no-systemd      # 无 systemd 环境：跳过单元安装，打印手动运行命令
./install.sh --uninstall       # 停用并移除用户单元（保留 data/ 和配置）
```

`install.sh` 做的事：检查 python3 ≥ 3.10 → 复制 `console.config.example.json` 为 `console.config.json`（已存在则不覆盖，幂等）→ 生成 `data/console.token`（0600）→ 把 `systemd/*.service` 模板里的 `%B_PLACEHOLDER` 渲染为 bundle 绝对路径后安装到 `~/.config/systemd/user/`（user unit 不支持 `%b`，所以用 sed 渲染）。

## 免 root 说明

- 一切写入只发生在 bundle 目录内的 `data/`（health.json、历史、令牌）。不碰 `/var/www`、`/etc`、`/run`。
- 服务以 **systemd user unit** 运行（`systemctl --user`），绑定 `127.0.0.1`（非特权端口 8090），无需任何系统级安装。
- systemd 单元有意只启用 `NoNewPrivileges`、**不加** `ProtectHome`/`ProtectSystem`：卡片脚本常放在 `$HOME` 下（如 `<旧部署路径>/deploy/`），收紧挂载视图会挡掉合法执行；如需纵深防御，请自行评估后在渲染出的 unit（`~/.config/systemd/user/`）里追加。
- 默认只在你的登录会话存续期间运行。若需**开机常驻（无人登录）**，一次性开启 linger：

  ```bash
  loginctl enable-linger $USER
  ```

  这一步走 polkit，可能需要一次管理员授权（这是整个方案里唯一可能涉及特权的可选项）。不开启也完全可用。

## 硬件自适应矩阵

daemon 插件按探测结果输出 `health.json`；探测不到的段，键**整段不出现**，前端隐藏整卡。

| 目标设备形态 | 探测方式 | 缺失时表现 |
|---|---|---|
| 笔记本（无 GPU / 无 BMC / 无台架电源） | 启动时探测 `nvidia-smi`、`ipmitool` 等，失败即禁用插件 | health.json 无 `gpus` / `fans` / `power` 键；页面只剩 Server Load + links |
| 服务器 + NVIDIA GPU | `nvidia-smi` 探测成功 | `gpus[]` 完整上报（温度/显存/PCIe/功耗，数组长度决定 tile 数） |
| 服务器无 BMC | `ipmitool` 本地探测失败 | ipmi 插件整段禁用，`fans[]` / `power{}` 不出现，进程不崩 |
| 服务器有远程 BMC | 配置 `plugins.ipmi.bmc = {host, user, password_env}` | 凭据只经环境变量传递，不落盘 |
| LLM 推理服务在跑 | llm 插件端口/进程探测 | 未检测到时 `llm.backend: null`，数值诚实降级 |

## 控制卡配置（cards）

控制卡是通用执行器：**server 端零模型特判**，一切业务逻辑写在你自己的脚本里。`console.config.json` 的 `cards[]` 约定：

```json
{
  "id": "model-a",
  "name": "示例模型 A",
  "icon": "gpu",
  "timeout_s": 90,
  "scripts": {
    "status": "deploy/model-a/launch.sh status",
    "start":  "deploy/model-a/launch.sh start",
    "stop":   "deploy/model-a/launch.sh stop"
  }
}
```

- `scripts.status` 的 **stdout 必须是 JSON**，其中 `state` 字段枚举必须完整覆盖：`ready` / `running` / `starting` / `loading` / `stopped` / `unknown`（前端按钮状态机按此全量映射）。
- 互斥编排（如"先停对端、等 VRAM 释放、再 re-route"）写在 start/stop **脚本内部**，控制台只负责调用与透传。
- `scripts` 里的相对路径一律相对 bundle root 解析；把脚本连同 bundle 一起拷走即可。
- 卡片的 start/stop 脚本若会拉起**常驻进程**，务必重定向并脱离 stdout（如 `nohup ... >/dev/null 2>&1 &`，或用 `docker run -d` 这类容器化启动）；否则 server 会等待管道关闭，最长拖满 `timeout_s`（默认 90s）才返回。

## 迁移本机（旧模型控制服务）的步骤

1. 编辑 `console.config.json`，把旧控制服务的 launch 脚本路径（`/opt/<model>-control` 风格；绝对路径，或将脚本拷入 bundle 的 `deploy/` 后写相对路径）填进 `cards[].scripts`。
2. 按本机实际情况调整 `links[]`（Wiki:3000、Open WebUI:3001、Frigate:5000、文件同步:8080、Authentik:9000、Cockpit:9090 等，url 中 `{host}` 由前端用 `location.hostname` 替换）。
3. `systemctl --user restart console-daemon console-server`。
4. 对照现行控制台导出的 health.json 的顶层键验证兼容（gpus / system / llm / power / fans 全有）。

bundle 内的 `daemon/ + server/ + web/` 三件套即为现行 portal 采集 + 控制面的替代品；本机现行控制台保持不动、继续运行。

## 反向代理（对外暴露）

默认只监听 `127.0.0.1:8090` —— 这正是 root-free 的边界：不绑特权端口、不装系统级反代、不签发证书。若要对外访问，需自行在设备上（或以 root）套一层带 TLS 的反代，把流量转到 127.0.0.1:8090：

```caddyfile
# Caddyfile 片段
console.example.com {
    reverse_proxy 127.0.0.1:8090
}
```

```nginx
# nginx 片段
server {
    listen 443 ssl;
    server_name console.example.com;
    # ssl_certificate     /etc/letsencrypt/live/console.example.com/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/console.example.com/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## 令牌鉴权

- `install.sh` 生成 `data/console.token`，权限 **0600**（优先由 `python3 console.py token` 生成，缺席时用 `/dev/urandom` 兜底）。
- 只读接口（`/`、`/health.json`、`/api/cards`、状态查询）无需令牌；**POST 控制接口**（start/stop）必须携带请求头 `X-Control-Token: <token>`，服务端用常数时间比较（`hmac.compare_digest`）校验，缺失/不匹配返回 403。
- `GET /api/cards/<id>/status` 免令牌可读（设计使然，页面要展示状态），但**每次 GET 都会 exec 一次 status 脚本**：默认只绑 `127.0.0.1` 无碍；若把 `listen.host` 改绑对外地址（且没有反代限流），status 脚本必须保持轻量快速。
- 换令牌：删除 `data/console.token` 后重跑 `./install.sh`，或 `python3 console.py token` 重写。

## 排除项声明

本 bundle **不包含**任何本机私有项目与业务内容（有意排除，非遗漏）：其它工程项目的专属服务与产物、个人站点、业务系统的配置与数据，一概不在包内。
