# FD/EMFILE 采样网关接入演练

一个本地演练系统：FastAPI 控制面管理一个低 `RLIMIT_NOFILE` 的 Linux TCP 回显进程。通过 API 打开并持有样例日志文件描述符，直到新连接的 `accept(2)` 实际返回 `EMFILE`；同时验证旧连接继续回显、新探测在 1 秒内收到 EOF/RST、接入线程不空转，以及关闭样例文件后原监听套接字自动恢复。

## 启动

```bash
docker compose up --build
```

- FastAPI/Swagger：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/healthz>
- TCP 回显端口：`127.0.0.1:9000`

Compose 中的 `drill` 服务会启动 Uvicorn API，并由 API 拉起 `app/echo_worker.py`。回显进程在创建监听套接字前把自己的 `RLIMIT_NOFILE` 降到 `DRILL_NOFILE_LIMIT`（默认 `32`）。

## 一键演示全部关键断言

最快的服务端完整自检（一个请求完成旧连接、EMFILE 探测、计数、CPU 和恢复验证）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/drill/self-test | python3 -m json.tool
```

也可以从宿主机运行独立 TCP 演示脚本，验证 Compose 端口映射：

```bash
python3 scripts/demo.py
# 或依次执行服务端自检和宿主机 TCP 演示：
./scripts/demo-requests.sh
```

脚本只使用 Python 标准库，依次完成：

1. 检查 API 和回显进程健康。
2. 记录初始 EMFILE 计数。
3. 建立一条长连接并验证故障前回显。
4. 调用 `/api/v1/drill/exhaust` 打开样例日志直到描述符表满。
5. 在故障期间向旧连接发送数据并验证回显一致。
6. 只建立一条新探测连接，断言其在连接建立后 1 秒内收到 EOF 或 RST。
7. 通过 API 查询真实 `accept(2)` 返回 `EMFILE` 的累计次数。
8. 采样接入线程 CPU，确认没有忙等。
9. 调用 `/api/v1/drill/release` 关闭样例文件。
10. 用一条全新连接验证回显，确认原监听自动恢复。

可选参数：

```bash
python3 scripts/demo.py --api-base http://127.0.0.1:8000 \
                        --echo-host 127.0.0.1 \
                        --echo-port 9000
```

## 演示请求

JetBrains/VS Code REST Client 可直接使用 [`requests/demo-requests.http`](requests/demo-requests.http)。

等价的 curl 顺序：

```bash
# 健康与初始状态
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool
curl -s http://127.0.0.1:8000/api/v1/status | python3 -m json.tool

# 占满回显进程的文件描述符名额
curl -s -X POST http://127.0.0.1:8000/api/v1/drill/exhaust | python3 -m json.tool

# API 从同容器发起一条新探测；故障期期望 outcome=eof 或 reset
curl -s -X POST http://127.0.0.1:8000/api/v1/probe \
  -H 'Content-Type: application/json' \
  -d '{"send_payload": false, "timeout_seconds": 1.0}' | python3 -m json.tool

# 故障期间查询实际 EMFILE 次数
curl -s http://127.0.0.1:8000/api/v1/metrics/emfile | python3 -m json.tool

# 采样 1 秒接入线程 CPU；busy_spin_suspected 应为 false
curl -s 'http://127.0.0.1:8000/api/v1/drill/accept-loop-check?seconds=1.0' \
  | python3 -m json.tool

# 通过 API 关闭样例日志描述符
curl -s -X POST http://127.0.0.1:8000/api/v1/drill/release | python3 -m json.tool

# 恢复后新连接成功回显
curl -s -X POST http://127.0.0.1:8000/api/v1/probe \
  -H 'Content-Type: application/json' \
  -d '{"payload": "recovery-echo", "expect_echo": true}' | python3 -m json.tool
```

原始 TCP 行为可用 `nc 127.0.0.1 9000` 观察；完整的长连接保持和计时断言由 `scripts/demo.py` 执行。

## 主要 API

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| `GET` | `/healthz` | API 与受管回显进程健康状态 |
| `GET` | `/api/v1/status` | RLIMIT、PID/TID、监听 FD、样例 FD、FD 数、EMFILE 和回显指标 |
| `POST` | `/api/v1/drill/exhaust` | 打开样例日志直到 `open(2)` 返回 `EMFILE` |
| `POST` | `/api/v1/drill/release` | 关闭全部 API 打开的样例日志 FD |
| `POST` | `/api/v1/sample-files/open` | 打开指定数量，或 `{"fill_to_limit": true}` |
| `DELETE` | `/api/v1/sample-files` | 同 release |
| `GET` | `/api/v1/metrics/emfile` | 查询真实 accept EMFILE 累计次数 |
| `GET` | `/api/v1/drill/accept-loop-check?seconds=1` | 通过 `/proc` 采样接入线程 CPU/状态 |
| `POST` | `/api/v1/probe` | 发起一条 TCP 探测并分类 `eof`/`reset`/`echo`/`timeout` |
| `POST` | `/api/v1/drill/exhaust` | 打开样例日志直到 `open(2)` 返回 `EMFILE`；自检运行中返回 **409** |
| `POST` | `/api/v1/drill/release` | 关闭全部 API 打开的样例日志 FD；会立即打断运行中的自检 |
| `POST` | `/api/v1/sample-files/open` | 打开指定数量，或 `{"fill_to_limit": true}`；自检运行中返回 **409** |
| `DELETE` | `/api/v1/sample-files` | 同 release |
| `GET` | `/api/v1/metrics/emfile` | 查询真实 accept EMFILE 累计次数 |
| `GET` | `/api/v1/drill/accept-loop-check?seconds=1` | 通过 `/proc` 采样接入线程 CPU/状态 |
| `POST` | `/api/v1/probe` | 发起一条 TCP 探测并分类 `eof`/`reset`/`echo`/`timeout` |
| `GET` | `/api/v1/drill/self-test` | 查询当前/最近一次自检状态与（含 aborted 的）报告 |
| `POST` | `/api/v1/drill/self-test` | 服务端执行完整旧连接/EMFILE/EOF/非忙等/恢复断言；已有手动样例或自检并发时 **409** |
| `POST` | `/api/v1/drill/restart` | 重启受管回显进程并重置其进程内计数；会立即打断运行中的自检 |

为方便手工调用，也提供无版本别名：`/api/status`、`/api/emfile-count`、`/api/fill`、`/api/close`、`/api/probe`、`/api/self-test`、`/api/drill/exhaust`、`/api/drill/release`。

## 并发、409 与“打断自检”语义

为修复“自检失败收尾误关值班员手动样例 FD、旧自检关掉释放/重启后新开的样例”的问题，所有样例操作与自检现在由 `app/coordination.py` 中的 `SelfTestManager` 统一仲裁：

1. **发起自检的前置条件**：回显进程当前 `open_sample_files == 0`（没有任何遗留/手动样例），且没有另一个未结束的自检。否则 `POST /api/v1/drill/self-test` 返回 **409**：
   - `code=self_test_running`：已有自检在跑；
   - `code=self_test_stopping`：上一个自检已被打断、正在收尾（稍后重试即可）；
   - `code=manual_samples_open`：已有手动样例，响应带 `open_sample_files` 与 `sample_owners`。被拒绝的自检不会关闭这些样例。
2. **自检进行中**：`POST .../self-test`、`POST /drill/exhaust`、`POST /sample-files/open`（含 `fill_to_limit`）一律 **409**（`code=self_test_running`）；`GET /api/v1/status`、`GET /api/v1/drill/self-test`、指标与探测不受影响，仍可查询/调用。
3. **手动 release / DELETE 样例 / restart 始终允许且立即执行**：它们先把当前自检标记为 `abort`，再执行关闭全部样例或重启。CPU 采样的 0.5 秒 `sleep` 与 TCP 探测不持有仲裁锁，因此 release/restart **不需要等探测或整个自检结束**（实测 release 约 3–4 ms、restart 约 0.1 s 即返回）。响应中的 `self_test_interrupted` 给出被打断的 `run_id`。
4. **操作完成后可立即开新样例**：一旦 abort 已置位，即使旧自检线程尚未完全退出，手动开样例也会被放行。
5. **被打断的自检报告中止，迟到步骤不得再碰样例**：每个自检有唯一 `run_id` 与 `owner=selftest:<run_id>` 标签，并记录启动时的 worker `generation`。自检的每个 worker 命令/CPU 采样都经过“检查 abort/generation + 发命令”的原子门控；release/restart 持同一把锁完成“置 abort + 操作”，消除 check-then-act 竞争。打断后旧自检的后续步骤（含收尾 release）不会再到达 worker。原自检 HTTP 响应为 `200`，报告含 `"state":"aborted"`、`"aborted":true`、`"abort_reason"`，并同时在 `detail`/`report` 字段里放了同一份报告以兼容不同解析方式；事后也可用 `GET /api/v1/drill/self-test` 查到该中止报告。
6. **未被打断的自检，无论成败，只关自己的描述符**：自检 exhaust 打开的每个样例都带 `owner=selftest:<run_id>`，成功收尾与失败 `except` 收尾都调用带 `{"owner": ...}` 过滤的 `release_samples`，worker 只关闭同标签 FD。无 `owner` 的 release（值班员手动 release/DELETE）仍是“关闭全部”。`/api/v1/status` 新增 `sample_owners` 计数可核对归属。

### 手动 API 演示打断自检

```bash
# 可断言的一键场景套件（仅标准库；覆盖下面所有 409/abort/FD 存活断言）
python3 scripts/demo_interrupt.py
# 指向其它地址：
python3 scripts/demo_interrupt.py --api-base http://127.0.0.1:8000

# 纯 curl 版，完整演示“后台跑自检 -> 409 矩阵 -> release/restart 打断 -> 新样例存活”
./scripts/demo-interrupt-requests.sh
```

JetBrains/VS Code REST Client 的同名流程已加入 [`requests/demo-requests.http`](requests/demo-requests.http) 的 “Concurrency / interruption rules” 分段（A–I）。

## 实现行为

低上限下典型 FD 布局为：

```text
0,1,2      stdin/stdout/stderr（stdin/stdout 是 JSON 控制管道）
3          同一个 TCP listener，整个故障期间不重建
4          预留 /dev/null 描述符
5          故障前建立的旧回显连接
6..31      样例日志文件（RLIMIT_NOFILE=32 时）
```

关键机制：

1. **真实 EMFILE**：接入线程在非阻塞 listener 上经 `select()` 等待；可读后调用真实 `accept()`。描述符表满时内核返回 `EMFILE`，工作进程只在这个 `except errno.EMFILE` 分支增加 `emfile_total`。
2. **旧连接不受影响**：每条正常连接由独立回显线程服务，已分配的连接 FD 继续 `recv/sendall`。
3. **新探测快速结束**：发生 EMFILE 后，接入线程关闭预留 FD 4，用腾出的唯一名额接受排队中的探测连接，然后立即 `close()`。不发送数据的探测收到 FIN/EOF；若探测已写入未读数据，Linux 可能直接发 RST。两者均满足要求。
4. **不空转**：处理完一个 EMFILE 后回到阻塞式 `select()`。不能在表满时立刻再次 `accept()`：Linux 可能在发现队列为空前先遇到 FD 上限并持续返回 EMFILE，造成假象忙等。代码因此显式重新进入 `select()`；无新连接时线程状态为 `S`，CPU 采样为 0。
5. **自动恢复**：API 关闭样例文件后腾出大量 FD；listener 仍是 FD 3，`select()`/`accept()` 路径无需重启即可接受新连接并在线程中回显。
6. **故障期间可查询**：FastAPI 进程不受低 NOFILE 影响；工作进程的状态请求走已有 stdin/stdout FD。状态读取 `/proc/self/fd` 若自身遇到 EMFILE，会捕获并报告“描述符表已满”，不会导致状态接口失败。

## 本地无第三方依赖验证

工作进程有一个纯标准库集成检查：

```bash
python3 tests/worker_smoke.py
```

它会使用随机本地端口、`RLIMIT_NOFILE=32` 启动工作进程，并自动执行上述全部核心 TCP/FD 断言。若环境中有 pytest，也可运行 `pytest tests/test_emfile_worker.py`。

## 本次修正了什么（自检与值班员操作的并发/归属）

- 新增 `app/coordination.py`：`SelfTestManager` 作为进程内唯一仲裁者，提供开始前置检查、运行登记表、abort 事件、worker `generation` 栅栏，以及“检查 + 发 worker 命令”原子门控。
- `app/main.py`：lifespan 挂载 manager；新增 409 处理器；`POST /self-test` 在有手动样例或并发自检时返回 409；exhaust/open 在自检存活期返回 409；release/DELETE/restart 先打断自检再执行；新增 `GET /api/v1/drill/self-test`；`/api/v1/status` 增加 `self_test` 快照，worker status 增加 `sample_owners`。
- `app/selftest.py`：每次运行有 `run_id/owner`；所有 worker 命令与 CPU 采样走门控；识别 `SelfTestAborted` 并输出 `state=aborted/aborted=true/abort_reason`；被打断后不再发任何 open/release；未打断的成功与失败收尾都只发带 `{"owner": ...}` 归属过滤的 release。
- `app/echo_worker.py`：样例记录新增 `owner`（默认 `manual`，旧协议不受影响）；`release_samples(owner=...)` 只关同标签 FD，不带 owner 仍关闭全部；status 增加 `sample_owners`；修正归属过滤 release 响应里 `open_sample_files` 原来硬编码为 0 的问题。
- `app/controller.py`：新增每次成功启动递增的 `generation`；stdout 读线程按代捕获队列，避免旧 reader 的 EOF 哨兵在 restart 后误入新队列。
- 新增回归/演示：`tests/test_selftest_coordination.py`（7 个用例，含真实 FD 归属隔离）、`scripts/demo_interrupt.py`（可断言全套场景）、`scripts/demo-interrupt-requests.sh`（纯 curl 手动打断演示），并扩充 `requests/demo-requests.http`。

## 交付前实测结果（本轮，Python 3.11.2 / Linux）

本沙箱没有 Docker/Podman CLI 或容器 socket；用 bootstrap pip 把 `fastapi==0.165.0`、`uvicorn==0.54.0`、`httpx/pytest` 装到 `/tmp/pydeps`（未写入系统 Python、未改 `requirements.txt`），以等价的本地 Uvicorn + 受管 `echo_worker.py`（`DRILL_NOFILE_LIMIT=32`）实测。

- `python3 -m py_compile app/*.py tests/*.py scripts/demo_interrupt.py`：通过。
- `PYTHONPATH=/tmp/pydeps python3 -m pytest tests/ -q`：**8 passed**（原 `worker_smoke` 适配用例 + 7 个新增仲裁/归属用例）。
- `python3 tests/worker_smoke.py`：`WORKER SELFTEST PASSED`；`open_sample_files=26`、`open_fd_count=32`、`fd_table_full=true`；新探测 `EOF after 0.1 ms`；空闲故障 1 秒接入线程 CPU `0.0 ms`；release 26 个样例后新连接回显成功。
- 真实 HTTP 关键场景（`python3 scripts/demo_interrupt.py`，在 8000/8001/8002 多个等价实例上连续多轮）：**ALL INTERRUPTION SCENARIOS PASSED**，覆盖
  - 已有 2 个手动样例时 self-test → **409 `manual_samples_open`**，且被拒后仍为 2 个样例；
  - 自检运行中：第二个 self-test、`sample-files/open`、exhaust 均 **409**，`GET /status` 200 且 `self_test.active=true`；
  - release 打断：自检报告 `state=aborted, aborted=true`，原因 `operator called release_samples`，响应带匹配的 `self_test_interrupted=<run_id>`；release 返回后立刻 open 3 个成功，等待 0.8 s 后仍为 `open_sample_files=3 {'manual': 3}`，回显正常；
  - restart 打断：新 pid/新 generation，自检原因 `operator restarted the echo worker`，重启后立刻 open 的 2 个样例在旧自检结束后仍存活，回显正常；
  - 无打断自检：200、`ok=true`、六项 check 全 true、约 0.52 s，结束后 `open_sample_files=0`。
- `bash scripts/demo-interrupt-requests.sh`（纯 curl）：退出码 0；清晰打印预期 409、两种 aborted 报告以及 `SURVIVAL OK` / `post-restart samples survived ... = 2`。
- 并发与延迟加测（临时脚本，未入库）：6 线程栅栏同时 `POST /self-test` 恰好 **1 个 200（ok=true）+ 5 个 409**；自检 CPU 采样窗口中调 release **约 3.6 ms** 返回并成功打断，运行中 restart 约 **94 ms** 返回；连续 8 次“自检不同阶段 + restart”压力全部正确 abort、`/healthz` 与 `/status` 始终 200，穿插的完整自检也全部 ok；最终实例访问日志无 5xx / Traceback。
- `python3 scripts/demo.py --api-base ... --echo-port ...`：`DEMO PASSED`（原宿主机 TCP 时序断言无回归）。
- `docker-compose.yml` 用 PyYAML 解析通过（服务 `drill`，端口 `8000:8000`/`9000:9000`）；本轮**未改** `docker-compose.yml`/`Dockerfile`，新增 `app/coordination.py` 与脚本会被现有 `COPY app ./app`、`COPY scripts ./scripts` 自动带入镜像。

### 基线探测实测（原有行为未回归）

- API 探测实测：
  - 不发送数据：`outcome=eof`，`elapsed_after_connect_ms≈0.168`
  - 先发送 1 字节：`outcome=reset`，`errno=104 (ECONNRESET)`，约 `0.26 ms`
  - 恢复后：`outcome=echo`，`echo_matches=true`
- release 响应中原监听描述符保持不变：`listener_fd=3`
- 更早的突发测试：10 条并发探测均在约 2 ms 内收到 EOF，EMFILE/回收计数各增加 10，释放后回显正常。

## 未完成项与边界

- 本沙箱未安装 Docker/Podman、也没有可用容器 socket，因此**没有实际执行 `docker compose up --build`**；已验证 Compose YAML、Dockerfile 依赖项与等价本地 Uvicorn + worker 全链路。首次在有 Docker 的环境建议执行：
  ```bash
  docker compose up --build
  ./scripts/demo-interrupt-requests.sh
  python3 scripts/demo_interrupt.py
  python3 scripts/demo.py
  ```
- 被打断自检的“原请求最终响应”规范只要求“报告中止”、未规定状态码；当前返回 **200** 且报告含 `aborted=true / state=aborted`，并在 `detail`、`report` 字段放置同一报告以兼容不同解析；事后统一可用 `GET /api/v1/drill/self-test` 读取。若约定中止也要 4xx，可在 `app/main.py` 的 self-test 端点把该分支改为 `JSONResponse(status_code=409, ...)`，其余仲裁逻辑不变。
- 这是本地演练工具，未配置认证、TLS 或多副本；不应暴露到不可信网络。
- EMFILE 计数是工作进程生命周期内的累计值；调用 `/api/v1/drill/restart` 会重置。
- 样例日志文件默认保留在 Docker 命名卷 `drill-sample-logs` 中作为现场证据；如需清理可 `docker compose down -v`。
