# 盯盘 · KDJ 股票提醒

可自行部署的 A 股提醒网页应用，适合云服务器和小型 Docker 设备。Python + Flask + SQLite，无需 Redis、Node.js 或外部数据库；网页同时适配电脑和手机。

## 已实现

- 输入并保存股票代码，支持沪、深、北交易所代码识别。预置 `688605 / 先锋精科`。
- 每只股票独立选择日线、5 分钟、15 分钟。
- 默认 KDJ 参数 `N=9, M1=3, M2=3`，支持全局修改和每只股票覆盖。
- 两条默认规则：`J > 98 AND K > D`，`J < 2 AND K < D`。
- 可视化编辑规则：AND / OR；J、K、D、RSV、收盘价之间或与数值比较；支持 `>`、`>=`、`<`、`<=`、`=`、`!=`、向上穿越、向下穿越。
- 每条规则指定股票、周期、Bark 设备、触发方式、冷却时间、标题和正文。
- 多个 Bark 设备、官方或自建 Bark 服务、通知分组、铃声、推送等级、测试推送。
- KDJ 最新快照与最近 48 根曲线、行情采集错误、触发历史、每个设备的发送结果及失败重试。
- 管理密码登录、会话验证、SQLite 持久化、Docker 健康检查与日志轮转。

## Docker 部署

在装有 Docker Compose 的服务器上，把整个项目复制到一个目录，然后执行：

```sh
cp .env.example .env
# 编辑 .env：把 ADMIN_PASSWORD 改成你自己的密码（至少 12 位）
docker compose up -d --build
```

访问 `http://服务器IP:8080`，用 `.env` 中的管理密码登录。

1. 打开「Bark 设备」，填写服务地址与设备 Key，保存后点击「测试推送」。
2. 打开「自选股票」，添加或编辑股票、周期、KDJ 参数。
3. 打开「提醒规则」，调整阈值、比较关系、推送模板与触发方式。
4. 返回「行情概览」，点击「立即检查」，查看 K 线时间、指标和行情错误。
5. 「提醒记录」中可以查看实际触发与发送结果。

所有配置和记录位于 Docker 命名卷 `tixing-data`，容器更新后保留。不要使用 `docker compose down -v`，该命令会删除数据卷。

```sh
docker compose logs -f --tail=100
docker compose up -d --build     # 更新代码后重新构建
docker compose down             # 停止，保留数据
```

`.env` 的 `PORT` 用来修改宿主机访问端口；默认 `8080`。公网访问建议通过 Caddy、Nginx 等提供 HTTPS，并限制防火墙访问范围。使用 HTTPS 时设置 `COOKIE_SECURE=true`，反向代理需要保留原始 Host 请求头。密码、Bark Key 和数据库备份应仅由你自己保管。

### 玩客云 / ARMv7

Dockerfile 使用 `python:3.11-slim-bookworm`，设计上兼容 amd64、arm64 和 ARMv7，不依赖 pandas、NumPy 或 TA-Lib。MarkupSafe 的可选 C 扩展已关闭，便于小设备构建。在设备本机执行 `docker compose up -d --build` 会按设备架构构建。

如果在其他机器为 ARMv7 构建，需安装支持跨架构构建的 Docker Buildx / QEMU：

```sh
docker buildx build --platform linux/arm/v7 -t tixing:armv7 --load .
docker save tixing:armv7 -o tixing-armv7.tar
# 把 tar 复制到玩客云，再 docker load -i tixing-armv7.tar
```

导入后，在设备上创建 `compose.armv7.yaml`：

```yaml
services:
  tixing:
    image: tixing:armv7
    pull_policy: never
```

然后运行 `docker compose -f compose.yaml -f compose.armv7.yaml up -d --no-build`。默认内存上限 192 MB；小设备建议先监控 5～10 支股票，检查间隔 60～120 秒。需要可用的 Docker Linux 环境及能访问行情源、Bark 的网络。

本项目未在你的玩客云真机上运行过，硬件兼容性与实际内存使用请在设备上确认。

## 不用 Docker 运行

需要 Python 3.11 或更新版本。

Linux：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export ADMIN_PASSWORD='换成至少12位的管理密码'
python -m app
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:ADMIN_PASSWORD = '换成至少12位的管理密码'
.\.venv\Scripts\python.exe -m app
```

访问 `http://localhost:8080`。本地运行时数据库保存到 `./data/tixing.sqlite3`。可用 `DATA_DIR` 环境变量修改目录；`PORT` 修改监听端口。程序通过 Waitress 提供服务，不启用开发调试器。

本地启动不会自动读取 `.env`；`.env` 是给 Docker Compose 使用的。非 Docker 常驻部署可以使用 systemd、Supervisor 或系统服务管理程序。**请只启动一个 `python -m app` 实例**，多进程 / 多副本部署可能重复运行监控任务。不要给入口额外配置多个 Gunicorn worker。

本机测试遇到 Windows Python 3.14 的 WMI 系统信息查询卡住，影响 Waitress / pip 启动。若出现同样情况，可改用 `.\.venv\Scripts\python.exe tools/windows_python.py app` 启动；测试可用 `... tools/windows_python.py unittest discover -s tests -v`。此工具只绕过本机的 WMI 查询，Linux / Docker 不需要。

## 指标与提醒的准确含义

### KDJ 算法

```text
RSV = (当前收盘价 - 最近 N 根最低价) / (最近 N 根最高价 - 最近 N 根最低价) × 100
K = 上一次 K + (RSV - 上一次 K) / M1
D = 上一次 D + (K - 上一次 D) / M2
J = 3 × K - 2 × D
```

K、D 初值为 50；高低价相同时 RSV 为 50。先收集完整 N 根窗口，再递推计算。每次最多请求 320 根 K 线，至少要求 `N + 30` 根预热。J 可以大于 100 或小于 0，**不做截断**；条件比较使用原始精度，页面与通知显示两位小数。历史起点、复权方式、平滑参数不同，数值可能和券商软件有小幅差异，建议先与自己的看盘软件核对。

### 行情、交易时段与已收盘线

- 默认东方财富公开 K 线接口，失败时切换腾讯；也可以在页面固定数据源。全部采用**不复权**行情，避免日线与分钟线复权口径不一致。
- 公开接口无可用性、延迟或完整性保证。接口限流、改版、退市、上市历史不足或网络失败时，页面显示错误，**不会使用模拟行情代替**。
- 默认仅工作日 `09:30–11:32`、`13:00–15:06` 自动检查；午休和收盘留有数据发布缓冲。节假日不是完整交易日历判断，依靠“最新 K 线日期必须是当天”的校验抑制旧数据推送。
- 默认只判断已收盘线。分钟线按源接口标注的**结束时间**加 3 秒确认；日线在当天 `15:01` 后判断。日线在盘中会展示上一交易日快照，不用它触发当日推送。
- 关闭「只使用已收盘 K 线」可基于盘中数据提醒，但数值可能回撤；同根线仍只创建一次提醒。
- 分钟行情距当前时间超过“两个周期 + 5 分钟”（仅累计交易时间，扣除午休）时视为过期，避免用当日早先的旧行情触发当前提醒。
- 手动「立即检查」会绕过交易时段限制，**满足条件时会真实推送**；不绕过已收盘设置、去重、冷却或数据日期校验。
- 这是轮询提醒：只判断每次采集到的最新有效 K 线。关机、断网、暂停期间的每一根历史信号不会补发，检查周期内短暂出现后消失的信号也可能错过。

### 触发、防重与重试

- **进入条件时提醒（默认）**：条件从不满足变为满足时触发；首次观察到满足条件也会触发。持续满足时不会重复提醒。修改规则或股票会重置该项进入状态；同根 K 线的唯一约束依然有效。
- **每根 K 线**：每根最新 K 线满足条件即可触发；仍受冷却限制。需要 5 分钟线每根都提醒，可以设置该条规则冷却为 0 或 5 分钟。
- **冷却**：默认 30 分钟，按“规则 + 股票 + 周期”分别计时；规则可覆盖。冷却中进入条件的信号会被略过，不在冷却结束后自动补发。
- **去重**：数据库对“规则 + 股票代码 + 周期 + K 线时间”建立唯一约束，重启后仍生效。新提醒先持久化，再推送。
- **重试**：每个设备独立记录状态；成功设备不会因其他设备失败而重发。失败约 60 秒、300 秒后重试，最多 3 次。实际重试时点取决于检查间隔与任务耗时。超过 1 小时的信号停止重试。
- 股票、规则或接收设备停用 / 删除后，对应待发送消息会停止重试。
- 如果 Bark 已接收但响应在网络中丢失，应用无法确认，重试仍可能产生重复通知；Bark 接口不提供本应用可用的端到端幂等确认。
- 尚未配置设备时不消耗新信号的进入状态。没有启用的目标设备时不创建提醒记录。
- 历史保留 90 天，后台每次实际检查后清理。记录的是行情信号与推送服务确认，不能证明 iOS 必然展示了通知。

## Bark 配置与推送模板

App Store 安装 **Bark**，打开后显示类似：

```text
https://api.day.app/AbCdEf123456/这里是推送内容
```

在页面填写：

```text
服务地址：https://api.day.app
设备 Key：AbCdEf123456
```

自建服务使用自己的地址。发送采用 `POST /push`，Key 放在 JSON 的 `device_key` 字段，不放在 URL 中。页面不会返回已保存 Key 的原文；编辑时 Key 留空保持原值。Key 保存在服务器 SQLite 中，请妥善保管数据卷。

标题示例：`{name} · {rule}`

正文示例：

```text
{name} ({symbol}) · {period}
J={J} K={K} D={D}
价格：{close}
K线时间：{bar_time}
条件：{rule}
```

支持 `{symbol}`（含交易所）、`{code}`（六位代码）、`{name}`、`{period}`、`{rule}`、`{J}`、`{K}`、`{D}`、`{RSV}`、`{close}`、`{bar_time}`、`{time}`。不支持 Python 表达式或任意代码执行。股票名称可在页面自由设置，通知使用该名称。

## 数据备份

非 Docker 部署可在线导出一致性备份：

```sh
python -m tools.backup data/tixing.sqlite3 backup.sqlite3
```

Docker 部署：

```sh
docker compose exec tixing python -c "import sqlite3; s=sqlite3.connect('/app/data/tixing.sqlite3'); d=sqlite3.connect('/app/data/backup.sqlite3'); s.backup(d); d.close(); s.close()"
docker cp tixing:/app/data/backup.sqlite3 ./backup.sqlite3
```

备份含设备 Key、管理会话密钥和配置。SQLite 使用 WAL 模式，不要在服务运行时只复制主数据库文件；应使用上述在线备份，或停机后备份整个数据目录。

## 验证与排查

```sh
python -m unittest discover -s tests -v
python -m tools.check_market 688605
```

第二条会对两个数据源、三个周期逐一发起实际行情请求，并输出最新 K 线和 KDJ；请在目标服务器网络中运行。Docker 镜像只包含运行代码，诊断工具可在源码目录执行。

行情失败时先确认服务器能访问 `push2his.eastmoney.com`、`web.ifzq.gtimg.cn`、`ifzq.gtimg.cn`；检查代理、DNS、接口限流与是否可切换数据源。Bark 成功但手机没提示时，检查 iOS 通知权限、专注模式、网络及 Bark App 内归档。

应用仅提供自定义行情信号提醒，不自动下单。
