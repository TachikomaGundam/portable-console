# Portable Console — Design Contract

本 bundle 是现行门户控制台的**可移植免 root 重制版**。目标：笔记本 / 服务器（有无 BMC、有无 NVIDIA）都能部署，硬件项自适应探测，缺什么隐藏什么。本机现行控制台（/var/www、/opt、/etc/caddy）**一律不改**，本 bundle 与它并存。

## 0. 硬约束（所有实现必须遵守）

- 免 root：一切写入只发生在 bundle 目录内（`data/`）。不碰 /var/www、/etc、/run。
- 零第三方依赖：Python ≥3.10 标准库 only（http.server、subprocess、json、tempfile…）。前端零构建、零 CDN。
- 网络默认值一律 `127.0.0.1`；前端只用相对 URL；服务链接用 `{host}` 占位符（前端用 `location.hostname` 替换）。
- 诚实降级：探测不到的硬件段整段缺失（键不出现在 health.json），前端隐藏整卡；禁止假值/幽灵值（沿用 wiki llm-monitor 页 "honest-static vs dead" 规则）。
- 打包不包含任何本机私有项目内容（其它工程项目服务、个人站点、业务系统数据），有意排除，非遗漏。

## 1. 目录布局（bundle root = `console/`）

```
console/
  DESIGN.md / README.md
  console.py                 # CLI: daemon run|once, serve, card status|start|stop <id>, token
  console.config.example.json
  daemon/
    harness.py               # 主循环、history 滚动、usage 累加、原子写 JSON
    config.py                # 配置加载（bundle 相对路径解析）
    plugins/
      base.py                # Collector 协议 + probe 缓存
      system.py nvidia.py ipmi.py llm.py
  server/
    server.py                # 静态门户 + /api/* （http.server, ThreadingHTTPServer）
    control.py               # 通用控制卡执行器
  web/
    index.html               # 门户（从现行 90KB 版改造：数据驱动渲染）
  data/                      # 运行时生成，gitignore：health.json health.history.json
                             # usage-stats.json stats.json console.token(0600)
  systemd/
    console-daemon.service   # --user 单元模板
    console-server.service
  install.sh                 # 渲染配置 + 安装 user unit + 生成 token
  tests/                     # pytest，无网络、无 root、mock subprocess
```

## 2. 配置文件 `console.config.json`（示例见 console.config.example.json）

| 键 | 默认 | 说明 |
|---|---|---|
| `listen.host` | `"127.0.0.1"` | server 绑定 |
| `listen.port` | `8090` | server 端口（旧 8002/8004/3001 反代职责合并到这里） |
| `data_dir` | `"data"` | 相对 bundle root |
| `poll_interval_s` | `1` | daemon 周期 |
| `plugins.llm.non_llm_ports` | `[]` | 追加到内置黑名单 |
| `plugins.ipmi.bmc` | `null` | `{host,user,password_env}`；为空则尝试本地 ipmitool（失败→整段禁用） |
| `cards` | `[]` | 控制卡数组，见 §4 |
| `links` | `[]` | 门户卡片清单 `{name,url,icon,port_check}`, url 可含 `{host}` |
| `history.max_samples` | `720` | |

路径值（脚本、data_dir）：相对路径一律相对 bundle root 解析。

## 3. health.json / stats 输出契约（与现行兼容）

顶层键（缺段的键**不出现**）：
`cpu_cores load_1m load_5m load_15m mem_* swap_* disks[] disk_total disk_used disk_used_pct uptime_s`（system，恒在）、
`gpus[]`（nvidia）、`fans[] power{current_w,consumption_24h_kwh,consumption_30d_kwh}`（ipmi）、
`llm{…}`（llm，见 wiki llm-monitor 全量表；未检测到时 `llm.backend:null` 且数值诚实降级）、
`updated updated_iso`。
数组元素键名与现行完全一致：gpu = `card use_pct vram_alloc_pct temp_edge_c temp_junction_c temp_memory_c power_w vram_total_gb vram_used_gb pcie_gen_cur pcie_gen_max pcie_width_cur pcie_width_max`；fan = `name rpm`；disk = `path label total used free used_pct`。
写文件：tempfile + chmod 0644 + os.replace（原子）；`console.token` 0600。

## 4. 控制卡（通用化，替代写死的每模型 server.py）

```json
{"id":"model-a","name":"示例模型 A","icon":"gpu","timeout_s":90,
 "scripts":{"status":"<path> status","start":"<path> start","stop":"<path> stop"},
 "status_gpu": {"source":"scripts.status"}, "webui_port": null}
```
- status 脚本 stdout 为 JSON（state 字段枚举必须完整：`ready running starting loading stopped unknown`）。
- start/stop 互斥编排（先停对端、等 VRAM 释放、re-route）写在**脚本里**，server 不做模型特判。
- server 端点：
  - `GET /api/cards` → `{"cards":[{"id","name","icon","actions":["start","stop"]}]}`（无 token）
  - `GET /api/cards/<id>/status` → 透传脚本 JSON + `updated_at`
  - `POST /api/cards/<id>/start|stop` → 需 `X-Control-Token`；执行脚本，返回 `{"action","result","steps":[...]}`；未知 id 404、脚本缺失 500、超时 504。
  - `GET /api/links` → 渲染后的 links（`{host}` 由请求 Host 替换）。
- token 比较用 `hmac.compare_digest`。

## 5. 前端数据驱动规则

- GPU / 风扇 / 电源 / 磁盘 / 控制卡区块：数据键缺失 ⇒ `display:none` 整块；数组长度决定 tile 数（不再写死 GPU0-3、FAN1-10）。
- 笔记本常见形态：只有 system 段 → 页面只剩 Server Load + links。
- 轮询 1s；控制按钮状态机按 §4 state 枚举全量映射（wiki D1 教训：状态词表必须穷举）。

## 6. 验收（集成阶段执行）

1. `python3 console.py daemon --once` 在本机跑通，health.json 顶层键与现行控制台导出的 health.json 对齐（gpus/system/llm/power/fans 全有）。
2. 断网/无 IPMI/无 nvidia 环境用 mock 测：对应段缺失、进程不崩。
3. `serve` 后：`/`、`/health.json`、`/api/cards`、`/api/cards/<id>/status` 200；`POST` 无 token 403。
4. 前端在无 gpus/fans 的假 health.json 下隐藏对应区块（curl HTML + 手动 DOM 断言或 headless）。
5. 全部改动文件过 `python3 -m py_compile` / 现有 lint。
