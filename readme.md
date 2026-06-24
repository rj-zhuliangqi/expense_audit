# Expense Audit 拆分后的职责与启动方式

当前结构应理解成“两类服务 + 一个客户端 / worker”，不是三个 HTTP 服务。

## 职责划分

- 数据准备客户端 / worker：`expense_audit_orchestrator`
- 图运行时服务：`graph_runtime`
- 节点调用网关：`node_gateway`

其中 `expense_audit_orchestrator` 不再是 HTTP 服务。它应该被嵌入到队列消费程序、批处理任务或业务 worker 里，负责：

1. 从队列或业务系统拿到单据任务
2. 调 OCR、核销单接口和其他上游服务，准备 `preparedInput`
3. 调用 `graph_runtime` 服务执行图
4. 拿到结果后写数据库、回写状态、发后续事件

当前仓库里，`expense_audit_orchestrator` 是可复用的客户端库；`execute_graph.py` 只是这个客户端库的本地 CLI 调试入口；`graph_runtime` HTTP 服务统一通过 `graph_runtime.api:create_app` 启动，不代表生产形态应该对外提供一个审单 HTTP 服务。

如果需要本地 mock 上游业务接口，再单独启动：`mock_server.py`

## 推荐端口

- 图运行时服务：`8090`
- 节点调用网关：`8091`
- mock 上游服务：`8080`

## 服务启动

### 1. 启动 mock 上游服务

```bash
cd /mnt/d/gorules/expense_audit
.venv/bin/python mock_server.py
```

### 2. 启动图运行时服务

```bash
cd /mnt/d/gorules/expense_audit
.venv/bin/python -m uvicorn graph_runtime.api:create_app --factory --host 127.0.0.1 --port 8090
```

启动后可用接口：

```bash
curl -sS http://127.0.0.1:8090/health

curl -sS -X POST http://127.0.0.1:8090/api/v1/graph-runtime/evaluations \
  -H 'Content-Type: application/json' \
  -d '{"graphPath":"graph-latest-0610-2018.json","preparedInput":{"context":{"receiptCode":"REC-001"}},"includePreparedInput":true}'
```

### 3. 启动节点调用网关

```bash
cd /mnt/d/gorules/expense_audit
.venv/bin/python -m uvicorn node_gateway.api:create_app --factory --host 127.0.0.1 --port 8091
```

当前图里的 node 调用已经统一走：`http://127.0.0.1:8091/api/v1/node-gateway/llm/evaluate`

## 客户端 / worker 使用方式

### 0. 联调时先决定你现在在哪个阶段

按下面的顺序用脚本，最不容易混：

1. 只确认队列里有没有消息、核销单号对不对：用 `prepare_from_queue.py --receipt-code-only`
2. 确认“队列消息 -> 数据准备”这段是否打通：用 `prepare_from_queue.py --prepared-output-path ...`
3. 不经过队列，直接拿一个核销单号做完整图执行：用 `execute_graph.py --receipt-code ...`
4. 已经有 `preparedInput` JSON，只想验证图执行：用 `execute_graph.py --prepared-input-path ...`
5. 要跑常驻消费者、走完整链路：用 `rabbitmq_worker.py`

建议先把第 1、2 步调通，再碰第 5 步。

### 1. 用独立客户端入口执行单据

客户端负责数据准备，并通过 `GRAPH_RUNTIME_URL` 或 `--graph-runtime-url` 调用下游 `graph_runtime`。如果不传 `--audit-service-url`，会默认复用本地 `8080` 的 mock 上游服务。

客户端不会再把本地 `graph_path` 原样透传给 runtime；而是先在本地读取图内容，再通过 HTTP 请求把 `graphContent` 发给下游 runtime。这样客户端和 runtime 即使不在同一路径下，也能完全解耦。

```bash
cd /mnt/d/gorules/expense_audit
GRAPH_RUNTIME_URL=http://127.0.0.1:8090 \
.venv/bin/python execute_graph.py --graph-path graph-latest-0610-2018.json --receipt-code REC20260603001
```

### 2. 使用现成 preparedInput 直接执行图

```bash
cd /mnt/d/gorules/expense_audit
.venv/bin/python execute_graph.py --graph-path graph-latest-0610-2018.json --prepared-input-path prepared-input2.json
```

### 3. 只导出 preparedInput JSON

导出的 `preparedInput` 会自动把 `operator_city.csv` 装入 `serviceData.telecom_list`，格式与 `prepared-input2.json` 保持一致，方便在 UI 中直接模拟。

```bash
cd /mnt/d/gorules/expense_audit
.venv/bin/python execute_graph.py \
  --graph-path graph-latest-0610-2018.json \
  --receipt-code REC20260603001 \
  --prepare-only \
  --prepared-output-path prepared-input.json
```

## 金蝶 OCR Provider

`create_receipt_audit_service(...)` 现在强制使用金蝶 OCR provider 作为 `ReceiptDataPreparer` 的默认 OCR 实现。

金蝶 OCR 配置会从项目根目录的 `.env` 读取，`os.environ` 中已有的同名变量仍然优先生效。

必填配置示例：

```dotenv
KINGDEE_OCR_BASE_URL=https://your-kingdee-host
KINGDEE_OCR_APP_ID=RJ_EMS
KINGDEE_OCR_APP_SECRET=your-app-secret
KINGDEE_OCR_ACCOUNT_ID=19xxxxxxxx
KINGDEE_OCR_TENANT_ID=ruxxxxxx
KINGDEE_OCR_USER=RJ_EMS
```

可选配置：

```dotenv
KINGDEE_OCR_USER_TYPE=UserName
KINGDEE_OCR_LANGUAGE=zh-CN
KINGDEE_OCR_BILL_TYPE=er_dailyreimbursebill
KINGDEE_OCR_VERIFY_FLAG=1
KINGDEE_OCR_UPLOAD_FILE_TYPE=1
KINGDEE_OCR_RECOGNITION_FILE_TYPE=1
KINGDEE_OCR_TIMEOUT=30
```

这个 provider 会在内部完成四步调用：获取 `AppToken`、获取 `accessToken`、上传文件、发起 OCR 识别。规则引擎侧最终只接收标准化后的 OCR 结果，不会暴露中间 token 或上传响应。

如果未配置上述必填环境变量，或者金蝶接口调用失败，流程会直接失败，不会再回退到本地 sample OCR provider。

## 生产形态建议

生产上建议不要暴露 `expense_audit_orchestrator` 的 HTTP API，而是：

1. 队列消费者收到单据任务
2. 在 worker 进程里调用 `create_receipt_audit_service(...)`
3. 先由 `ReceiptAuditService.prepare_receipt(...)` 完成数据准备，再由 `ReceiptAuditService.process_prepared_receipt(...)` 或 `ReceiptAuditService.process_receipt(...)` 完成下游 runtime 调用
4. 把结果写数据库并更新任务状态

如果 worker 需要调远端 runtime，设置环境变量 `GRAPH_RUNTIME_URL` 即可。

这里的职责边界建议固定下来：

1. RabbitMQ 中的消息仍然保持核销单级别。
2. 发票级别的迭代循环由 `ReceiptAuditService.process_prepared_receipt(...)` 统一控制，不要在 worker 里自行拆循环。
3. `invoiceResults` 就是后续入库的稳定边界；每条结果都包含发票键、preparedInput、decisionOutput、runtimeResult，以及 `executionStatus`、`decisionStatus`、`errorMessage` 等执行元数据。
4. 如果需要逐张发票即时落库或发事件，优先通过应用层的 `invoice_result_sink` 注入点处理，而不是把持久化逻辑塞进 RabbitMQ 基础设施代码。

## RabbitMQ worker

仓库里新增了 `rabbitmq_worker.py`，直接对应 Java 配置里的交换机、普通队列、月结队列、延时队列和延时处理队列。

消息体支持两种格式：

1. 纯文本单号，例如 `REC20260603001`
2. JSON，例如 `{"receiptCode": "REC20260603001"}` 或 `{"instanceCode": "REC20260603001"}`

消费成功后会 `ack`；解析失败或下游执行失败会 `nack(requeue=False)`，行为和 Java 里的 `setDefaultRequeueRejected(false)` 对齐。

当前 worker 会优先走两段式总控：

1. `prepare_receipt(...)` 做整单数据准备
2. `process_prepared_receipt(...)` 执行后续 runtime
3. `receipt_result_sink` 在整单汇总后自动组装 `ResultAuditInfoDTO`，并回调真实 `audit-info-save`

其中第 2 步在应用层内部会按发票逐条调用 graph runtime，并把结果聚合为 `invoiceResults` 列表。默认是串行执行，后续如果要扩展并发，也应继续沿着这个应用层边界演进，而不是把发票调度逻辑放回 worker。

如果注入的 service 不支持这两个接口，才会回退到旧的 `process_receipt(...)` 或 `evaluate(...)` 方式。

默认情况下，`rabbitmq_worker.py` 创建的主链路 service 会开启真实回写：

1. 使用 `expense_audit_orchestrator.writeback.assemble_result_audit_info(...)` 组装整单回写 payload
2. 使用与 `--audit-service-url` 相同的网关根地址，POST 到 `/api/audit-service/audit/audit-info-save`
3. 如果回写失败，异常会继续上抛到 worker，当前消息会 `nack(requeue=False)`
4. 如果未显式提供真实 `--audit-service-url`，或者地址仍指向 `127.0.0.1` / `localhost` / `0.0.0.0`，worker 会在启动阶段直接失败，不再回退到本地 mock 审单服务

主链路启动示例：

```bash
cd /mnt/d/gorules/expense_audit
set -a && source .env && set +a
GRAPH_RUNTIME_URL=http://127.0.0.1:8090 \
.venv/bin/python rabbitmq_worker.py \
  --amqp-url "$RABBITMQ_URL" \
  --audit-service-url https://service-uate-gw.ruijie.com.cn \
  --graph-path graph-latest-0616-1505.json \
  --queues audit
```

如果你要跑真实主链路，不要用本地 mock 数据地址，直接把 `--audit-service-url` 指向真实网关，例如：

```bash
cd /mnt/d/gorules/expense_audit
set -a && source .env && set +a
GRAPH_RUNTIME_URL=http://127.0.0.1:8090 \
.venv/bin/python rabbitmq_worker.py \
  --amqp-url "$RABBITMQ_URL" \
  --audit-service-url https://service-uate-gw.ruijie.com.cn \
  --graph-path graph-latest-0616-1505.json \
  --queues audit
```

如果你需要联调时把准备数据和回写 payload 同时落盘，可再加两个可选参数：

```bash
cd /mnt/d/gorules/expense_audit
set -a && source .env && set +a
GRAPH_RUNTIME_URL=http://127.0.0.1:8090 \
.venv/bin/python rabbitmq_worker.py \
  --amqp-url "$RABBITMQ_URL" \
  --audit-service-url https://service-uate-gw.ruijie.com.cn \
  --graph-path graph-latest-0616-1505.json \
  --queues audit \
  --prepared-output-dir output/worker-debug/prepared \
  --writeback-output-dir output/worker-debug/writeback
```

开启后会生成：

1. `output/worker-debug/prepared/<receiptCode>.prepared-receipt.json`
2. `output/worker-debug/writeback/<receiptCode>.writeback-payload.json`

`writeback` 文件会在真实回写前先落盘，所以即使 `audit-info-save` 失败，你也可以拿这个 payload 继续联调。

这条命令对应的完整路径是：

1. 从 `audit_ai_verification_queue` 取核销单消息
2. 调真实上游准备整单与发票数据
3. 调 `graph_runtime` 执行逐票 runtime
4. 自动回写 `audit-info-save`
5. 成功则 `ack`，失败则 `nack(requeue=False)`

延时队列 TTL 默认读取 `AUDIT_DELAY_TIME_MILLIS`，未设置时使用 `300000` 毫秒。

## 单次联调脚本

如果你只想联调“队列取任务 -> 解析核销单号 -> 做数据准备”这段，不想跑后面的 runtime/AI，可使用 `prepare_from_queue.py`。

当前 AI 核验链路的推荐拓扑是：

1. exchange：`audit_exchange`
2. routing key：`audit_ai_verification_routing_key`
3. queue：`audit_ai_verification_queue`

默认行为：

1. 强制加载项目根目录 `.env`
2. 从指定队列只取一条消息
3. 调 `ReceiptAuditService.prepare_receipt(...)`
4. 默认 `requeue=True` 放回队列，避免联调时误吞消息

### 什么时候执行这个脚本

1. 你已经确认消息能投到 `audit_ai_verification_queue`。
2. 你想验证“从队列里拿到核销单号后，能不能把上游数据准备齐”。
3. 你还不想跑 `graph_runtime`、`node_gateway` 或后续 AI 节点。

### 只核对队列消息中的核销单号

适用场景：

1. 先看队列里到底有没有消息。
2. 先确认拿到的核销单号是不是你预期的那条。

```bash
cd /mnt/d/gorules/expense_audit
set -a && source .env && set +a
.venv/bin/python prepare_from_queue.py \
  --queue audit_ai_verification_queue \
  --receipt-code-only
```

### 用本地 mock 上游做“队列 -> 数据准备”联调

先启动本地 mock：

```bash
cd /mnt/d/gorules/expense_audit
.venv/bin/python mock_server.py
```

再执行：

```bash
cd /mnt/d/gorules/expense_audit
set -a && source .env && set +a
.venv/bin/python prepare_from_queue.py \
  --queue audit_ai_verification_queue \
  --prepared-output-path output/prepared-receipt.json
```

### 用真实网关做“队列 -> 数据准备”联调

适用场景：

1. 队列消息已经是 UAT / 真实环境的核销单号。
2. 你希望调真实的核销单、发票文件、发票文件详情、费用项发票类型、发票占用信息接口。

```bash
cd /mnt/d/gorules/expense_audit
set -a && source .env && set +a
.venv/bin/python prepare_from_queue.py \
  --queue audit_ai_verification_queue \
  --prepared-output-path output/prepared-rjw260615000002.json \
  --audit-service-url https://service-uate-gw.ruijie.com.cn/
```

上面这条命令当前已经验证通过，执行成功后会导出 `prepared_receipt` JSON。真正给下游 runtime / AI 用的数据在：

1. `invoicePreparations[0].preparedInput`
2. 如果一张核销单下有多张票，则每张票各有一份 `preparedInput`

### 调通后什么时候 ack

默认不传 `--ack-on-success`，脚本会把消息重新放回队列，适合反复联调。

只有在下面两件事都确认之后，才建议加 `--ack-on-success`：

1. 核销单号拿对了。
2. `prepared_receipt` 里的数据结构和内容都符合预期。

示例：

```bash
cd /mnt/d/gorules/expense_audit
set -a && source .env && set +a
.venv/bin/python prepare_from_queue.py \
  --queue audit_ai_verification_queue \
  --prepared-output-path output/prepared-rjw260615000002.json \
  --audit-service-url https://service-uate-gw.ruijie.com.cn/ \
  --ack-on-success
```

### 旧示例：只做数据准备并导出 JSON

```bash
cd /mnt/d/gorules/expense_audit
set -a && source .env && set +a
.venv/bin/python prepare_from_queue.py \
  --queue audit_ai_verification_queue \
  --prepared-output-path output/prepared-receipt.json
```

### 旧示例：只核对队列里的核销单号，不做数据准备

```bash
cd /mnt/d/gorules/expense_audit
set -a && source .env && set +a
.venv/bin/python prepare_from_queue.py \
  --queue audit_ai_verification_queue \
  --receipt-code-only
```

如果确认要把这条消息真正消费掉，再额外传 `--ack-on-success`。

## 常驻 worker 什么时候执行

`rabbitmq_worker.py` 适合在下面条件都满足后再启动：

1. 队列绑定已经确认无误。
2. `prepare_from_queue.py` 已经证明“队列 -> 数据准备”能跑通。
3. `graph_runtime` 已经启动并可用。
4. 如果图里要调节点网关，`node_gateway` 也已经启动并可用。

完整链路示例：

```bash
cd /mnt/d/gorules/expense_audit
set -a && source .env && set +a
GRAPH_RUNTIME_URL=http://127.0.0.1:8090 \
.venv/bin/python rabbitmq_worker.py \
  --audit-service-url https://service-uate-gw.ruijie.com.cn/ \
  --queues audit
```

## 回归测试

```bash
cd /mnt/d/gorules/expense_audit
.venv/bin/python -m unittest test_execute_graph.py test_main.py -v
```



