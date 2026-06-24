# RabbitMQ 队列原理笔记

这份笔记用来解释三个常见问题：能不能连上、有没有权限、队列为什么声明失败。

## 1. RabbitMQ 是什么 broker

RabbitMQ 里的 broker 可以理解成消息中间件服务器本身。客户端并不是直接“读数据库”，而是先连到 broker，再通过 broker 收发消息。

在这个项目里，worker 连接 broker 后，会做三件事：

1. 建立 AMQP 连接。
2. 打开 channel。
3. 声明交换机和队列，然后开始消费。

## 2. vhost 是什么

vhost 是 virtual host 的缩写，可以理解成 RabbitMQ 里的一个逻辑隔离空间。

同一个 RabbitMQ 实例里，可以有多个 vhost。不同 vhost 之间的交换机、队列、权限是隔离的。

这意味着：

1. 你能连上 RabbitMQ，不代表你能访问某个 vhost。
2. 你对某个 vhost 有权限，不代表对别的 vhost 也有权限。
3. 队列名相同，如果在不同 vhost 中，也不是同一个队列。

当前项目使用的连接串里，vhost 是 `/`。

## 3. x-message-ttl 是什么

x-message-ttl 是队列的消息存活时间，单位是毫秒。

意思是：消息进入这个队列后，如果超过这个时间还没被消费，就会过期。

例如：

1. 300000 表示 5 分钟。
2. 21600000 表示 6 小时。

这个参数常用于延时队列。消息先进入延时队列，到期后会被 dead-letter 到处理队列。

## 4. 声明队列是什么意思

声明队列不是“创建一个新名字”这么简单，而是向 broker 声明：

1. 这个交换机要存在。
2. 这个队列要存在。
3. 这个队列的属性是什么。
4. 这个队列要绑定到哪个交换机、哪个 routing key。

如果队列已经存在，RabbitMQ 会检查你这次声明的属性和已有属性是否一致。

如果不一致，就会报错，例如：

1. 队列已经存在，但你声明的 x-message-ttl 和现有值不一样。
2. 队列已经存在，但 durable、exclusive、auto_delete 等属性不一致。

这就是为什么我这次测试里，先能连上，但声明延时队列时失败了：broker 里已有的 audit_delay_queue 的 ttl 是 21600000，而我本地默认声明的是 300000。

### 4.1 重新声明为什么会生效

很多人容易把“改了 Python 代码”和“broker 里的拓扑已经变了”混为一谈，但这两件事不是一回事。

真正会让 broker 发生变化的是：

1. worker 连上 RabbitMQ。
2. 打开 channel。
3. 执行 exchange_declare。
4. 执行 queue_declare。
5. 执行 queue_bind。

也就是说，只有代码真正跑到 `declare_topology(...)`，broker 里的交换机、队列和绑定关系才会被创建或校验。

如果你只是把本地配置从 `audit_queue` 改成 `audit_ai_verification_queue`，但没有让 worker 连到 broker 并执行声明，RabbitMQ 服务端并不会自动知道这件事。

## 5. 连通、权限、声明 这三个检查分别看什么

### 5.1 连通

关注的是：

1. 域名能不能解析。
2. 端口能不能访问。
3. AMQP 连接能不能建立。

如果这里失败，通常是网络问题、地址写错、端口不通、服务没启动。

### 5.2 权限

关注的是：

1. 用户名密码是否正确。
2. 用户对目标 vhost 是否有权限。
3. 是否允许这个用户在该 vhost 上声明、绑定、消费队列。

如果连接成功但权限不足，通常会在打开 channel、声明队列、绑定交换机、消费消息时失败。

### 5.3 声明

关注的是：

1. 队列是否已经存在。
2. 已存在的队列属性是否和当前代码一致。
3. 交换机、队列、绑定关系是否能成功声明。

如果连通和权限都没问题，但声明失败，通常是“已有资源和当前声明不一致”。

## 6. 在这个项目里的实际流程

RabbitMQ worker 的实际处理顺序是：

1. 从环境变量读取 RABBITMQ_URL。
2. 建立 AMQP 连接。
3. 连接到指定 vhost。
4. 声明 exchange 和 queue。
5. 绑定 queue 到 exchange。
6. 开始消费消息。

对应代码在 [rabbitmq_worker.py](../rabbitmq_worker.py)。

这里可以把第 4、5 步理解成“把当前代码里的拓扑同步到 broker”。

例如当前 AI 核验链路里，代码会声明并绑定：

1. exchange：`audit_exchange`
2. routing key：`audit_ai_verification_routing_key`
3. queue：`audit_ai_verification_queue`

对应关系就是：

1. `audit_exchange` + `audit_ai_verification_routing_key` -> `audit_ai_verification_queue`

## 7. worker 是消费者吗

是，worker 就是消费者。

更准确地说：

1. producer 负责把任务消息发到 RabbitMQ。
2. queue 负责暂存消息。
3. worker 负责从 queue 里取消息并处理，这一端就叫 consumer，也就是消费者。

在这个项目里，`rabbitmq_worker.py` 里的 worker 做的事情就是：

1. 监听队列。
2. 收到消息后解析核销单号。
3. 先查询稽核任务列表，只处理存在 `systemIdentifier=4` 且 `anaStatus=0` 的任务。
4. 命中后先调用任务状态更新，把任务抢占为 `newStatus=1`（进行中）。
5. 只有抢占成功才调用后续单据处理逻辑。
6. 成功后 ack，失败后 nack。

### 7.2 当前前置门禁的 ack / nack 语义

当前 AI 稽核主链路不是“拿到消息就一定跑 prepare/runtime/writeback”，而是先做一次业务门禁。

处理顺序是：

1. 从消息体解析 `receiptCode` / `instanceCode`。
2. 调用 `task-info-list/{instanceCode}` 获取稽核任务列表。
3. 遍历任务，只要存在一条 `systemIdentifier=4` 且 `anaStatus=0`，就认为这笔单据可以继续。
4. 命中后调用 `task-status-update`，把任务状态改成 `1`（进行中）。
5. 只有状态更新成功，才进入 prepare、runtime、writeback。

当前 worker 对这一步的确认语义是：

1. 如果没有命中待处理的锐捷 AI 任务，属于业务短路，直接 ack，不再继续。
2. 如果命中了，但 `task-status-update` 返回业务失败，或者状态更新接口本身调用异常，记录日志后直接 ack，不再继续。
3. 如果上游查询阶段本身抛错，例如接口不可用、超时、返回非法结构，这仍然算处理失败，走 nack（`requeue=False`）。

这样做的目的，是把“这条消息当前不该由 AI 继续处理”和“这条消息处理过程中遇到了真实上游故障”区分开。

### 7.1 broker 和 worker 的区别

这两个词在排查时很容易混。

1. broker：RabbitMQ 服务端本身，负责保存 exchange、queue、binding、message。
2. worker：你自己的消费程序，例如 `rabbitmq_worker.py` 或 `prepare_from_queue.py`，它只是连到 broker 去声明资源、取消息、处理消息。

可以把它理解成：

1. broker 是仓库。
2. worker 是来仓库取货的人。

所以“broker 上有没有这个队列”和“worker 有没有连到这个队列”是两个层面的事。

## 8. 有多个队列时，从哪个队列取数据

不是“所有队列都同时乱取”，而是 consumer 明确订阅哪些队列，就从哪些队列取。

你现在这套配置里，worker 默认会消费这几个队列：

1. `audit_ai_verification_queue`
2. `audit_monthly_queue`
3. `audit_delay_process_queue`

代码里对应的是 `resolve_consumer_queues(...)` 和 `basic_consume(...)`。

### 8.1 为什么会有多个队列

因为不同类型的消息，处理时机不同：

1. `audit_ai_verification_queue`：AI 核验链路当前使用的主队列。
2. `audit_monthly_queue`：月结或特定周期任务。
3. `audit_delay_queue`：延时队列，消息先放这里，等过期后再转发。
4. `audit_delay_process_queue`：延时到期后真正被处理的队列。

如果还保留 `audit_queue`，它通常表示旧的普通审单链路或其他历史消费者使用的主队列；当前这次联调里，AI 核验主队列已经切到 `audit_ai_verification_queue`。

### 8.2 具体例子

假设现在来了三种消息：

1. 一条 AI 核验消息，routing key 是 `audit_ai_verification_routing_key`，通常进 `audit_ai_verification_queue`。
2. 一条月结消息，routing key 是 `audit_monthly_routing_key`，通常进 `audit_monthly_queue`。
3. 一条延时消息，先进入 `audit_delay_queue`，TTL 到期后再死信转到 `audit_delay_process_queue`。

worker 如果订阅了这三个队列，就会分别消费这三类消息。它不是自己“猜”该从哪里取，而是队列里有消息、且它对这个队列执行了 `basic_consume`，它才会拿到。

### 8.3 我们这个项目默认怎么取

当前 worker 默认订阅的就是：

1. `audit_ai_verification_queue`
2. `audit_monthly_queue`
3. `audit_delay_process_queue`

所以：

1. 你如果投 AI 核验消息，worker 会从 `audit_ai_verification_queue` 取。
2. 你如果投月结消息，worker 会从 `audit_monthly_queue` 取。
3. 你如果投延时链路消息，到期后 worker 会从 `audit_delay_process_queue` 取。

## 9. 你可以把它理解成一个快递站

可以把 RabbitMQ 想成一个快递站：

1. broker 是快递站本身。
2. vhost 是快递站里的一个独立仓库。
3. queue 是仓库里的某个货架。
4. producer 是送货的人。
5. consumer / worker 是取货的人。

如果你已经知道要取哪个货架，就直接盯着那个 queue；如果你同时订阅了多个货架，就会按订阅的顺序或并发规则从这些货架取货。

## 10. 我们这次测试的结论

1. 账号密码是用上的。
2. RabbitMQ 连接是成功的。
3. 失败点不在认证，而在队列声明时的 ttl 不一致。
4. 把 AUDIT_DELAY_TIME_MILLIS 调成 broker 当前已有值后，队列声明成功。

## 11. 排查建议

如果以后再连不通，按这个顺序查：

1. 先查 URL、主机名、端口。
2. 再查用户名、密码、vhost。
3. 再查队列和交换机的属性是否与 broker 已有对象一致。
4. 最后再查消息体和消费逻辑。

## 12. 补充 QA

### 12.1 交换机是什么意思

交换机（exchange）可以理解成 RabbitMQ 里的“分拣中心”。

producer 发消息时，消息先到 exchange，不是直接进 queue。exchange 再根据规则把消息路由到一个或多个 queue。

你可以把 queue 当货架，exchange 当分拣员：

1. producer 先把包裹交给分拣员（exchange）。
2. 分拣员按规则把包裹放到对应货架（queue）。

### 12.2 exchange、routing key 分别是什么

这两个是“消息怎么被分发”的核心参数：

1. exchange：分发器本身，决定采用哪种路由规则。
2. routing key：消息携带的路由标签，相当于“投递关键字”。

常见流程是：

1. producer 发送消息到某个 exchange，同时带上 routing key。
2. RabbitMQ 查看 exchange 上已有的绑定规则（binding key）。
3. 匹配成功的 queue 会收到这条消息。

在 direct exchange 下，可以先按“基本等于”来理解：

1. routing key = queue 绑定时的 key，则会投递到该 queue。
2. 不匹配则不会投递到该 queue。

例如：

1. exchange 是 `audit_exchange`。
2. `audit_ai_verification_queue` 绑定 key 是 `audit_ai_verification_routing_key`。
3. 你发送消息时 routing key 也是 `audit_ai_verification_routing_key`，消息就会进入 `audit_ai_verification_queue`。

### 12.3 binding 是什么

binding 可以理解成“交换机到队列的路由规则”。

在这个项目里，最关键的不是只记住 queue 名，而是记住完整关系：

1. exchange 是谁。
2. routing key 是谁。
3. 这个 routing key 绑定到了哪个 queue。

例如当前 AI 链路的 binding 就是：

1. `audit_exchange` + `audit_ai_verification_routing_key` -> `audit_ai_verification_queue`

### 12.4 一个 exchange、多个 routing key，可以怎么绑定

同一个 exchange 下完全可以有多个 routing key。

例如：

1. `audit_exchange` + `audit_routing_key` -> `audit_queue`
2. `audit_exchange` + `audit_ai_verification_routing_key` -> `audit_ai_verification_queue`
3. `audit_exchange` + `audit_monthly_routing_key` -> `audit_monthly_queue`

这在 direct exchange 下是很常见的做法。

### 12.5 同一个 queue 能不能绑定多个 routing key

可以。

例如：

1. `audit_exchange` + `audit_routing_key` -> `audit_queue`
2. `audit_exchange` + `audit_ai_verification_routing_key` -> `audit_queue`

这样两种 routing key 的消息都会进入同一个 `audit_queue`。

但这次联调里我们没有这么做，而是给 AI 核验单独用了 `audit_ai_verification_queue`，这样语义更清晰，也更容易监控和排查。

### 12.6 queue 名和 routing key 名能不能写成一样

技术上可以，但不推荐。

例如把 queue 名写成 `audit_ai_verification_routing_key` 并不是语法错误，RabbitMQ 也能工作，但会非常容易把“队列名”和“路由标签”混在一起。

这次联调里就出现过这个问题：

1. routing key 是 `audit_ai_verification_routing_key`
2. 错误地把 queue 名也改成了 `audit_ai_verification_routing_key`

结果就是：

1. 你以为自己在查 AI 队列。
2. 实际查到的是一个误声明出来的旧队列。

当前已经统一为：

1. exchange：`audit_exchange`
2. routing key：`audit_ai_verification_routing_key`
3. queue：`audit_ai_verification_queue`

### 12.7 为什么脚本看到队列空，但 worker 又消费到过消息

这通常有几种原因：

1. 你查的不是实际绑定的那个 queue。
2. 你查的是旧队列名，而 producer 已经发到新队列。
3. 另一个消费者比你先一步把消息取走了。
4. 你查的时候队列已经没有积压，但刚才短暂出现过消息。

这次联调里，真实原因依次出现过两种：

1. 先是查错了旧队列名 `audit_ai_verification_routing_key`。
2. 后来 worker 确实从正确路由上取到了消息，但 `prepare_from_queue.py` 查的是别的队列或查的时机已经在消息被取走之后。
