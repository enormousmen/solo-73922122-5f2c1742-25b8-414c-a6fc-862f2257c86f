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
| `POST` | `/api/v1/drill/self-test` | 服务端执行完整旧连接/EMFILE/EOF/非忙等/恢复断言 |
| `POST` | `/api/v1/drill/restart` | 重启受管回显进程并重置其进程内计数 |

为方便手工调用，也提供无版本别名：`/api/status`、`/api/emfile-count`、`/api/fill`、`/api/close`、`/api/probe`。

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

## 交付前实测结果

本次交付环境有 Python 3.11.2 与 GCC，但没有 Docker/Podman CLI 或容器 socket；因此实际试跑采用等价的本地 Uvicorn + 受管 echo worker。FastAPI 依赖被临时安装到 `/tmp/pydeps`，未写入系统 Python。

已通过的关键结果：

- `python3 tests/worker_smoke.py`：`WORKER SELFTEST PASSED`
  - `open_sample_files=26`（含一条旧连接时），`open_fd_count=32`，`fd_table_full=true`
  - 旧连接故障前/故障中回显均一致
  - 新探测：`EOF after 0.1 ms`（连接建立后计时，远小于 1 秒）
  - 单次探测后 `emfile_total=1`、`reaped_after_emfile_total=1`
  - 空闲故障 1 秒内接入线程 CPU：`0.0 ms`
  - release 关闭 26 个样例 FD 后，新连接回显成功
- `python3 scripts/demo.py` 对真实 FastAPI/Uvicorn 试跑：`DEMO PASSED`
- `POST /api/v1/drill/self-test` 试跑：`ok=true`，六项检查全部为 `true`；探测 EOF 约 `0.184 ms`，EMFILE 计数为 1，CPU 采样为 `0.0s`，恢复回显匹配
- 额外突发测试：10 条并发探测均在约 2 ms 内收到 EOF，EMFILE/回收计数各增加 10，释放后回显正常
- API 探测实测：
  - 不发送数据：`outcome=eof`，`elapsed_after_connect_ms≈0.168`
  - 先发送 1 字节：`outcome=reset`，`errno=104 (ECONNRESET)`，约 `0.26 ms`
  - 恢复后：`outcome=echo`，`echo_matches=true`
- release 响应中原监听描述符保持不变：`listener_fd=3`
- `docker-compose.yml` 已用 PyYAML 解析，服务、端口和环境变量结构有效；Python 文件全部通过 `py_compile`

## 未完成项与边界

- 本沙箱未安装 Docker/Podman，因此没有实际执行 `docker compose up --build`；已验证 Compose YAML、Dockerfile 依赖安装项和等价本地进程链路。首次在有 Docker 的环境运行时仍建议执行一次 `docker compose up --build` 和 `python3 scripts/demo.py`。
- 这是本地演练工具，未配置认证、TLS 或多副本；不应暴露到不可信网络。
- EMFILE 计数是工作进程生命周期内的累计值；调用 `/api/v1/drill/restart` 会重置。
- 样例日志文件默认保留在 Docker 命名卷 `drill_sample-logs` 中作为现场证据；如需清理可 `docker compose down -v`。
