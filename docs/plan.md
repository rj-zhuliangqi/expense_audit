推荐方案是：
RabbitMQ 只消费“核销单任务”，应用服务负责“拆发票、逐张执行、聚合结果、触发入库钩子”，这样后面无论要加部分成功、失败重试、并发执行还是分批入库，都不会污染基础设施层。

步骤

固化边界。把 application.py 定义成唯一的编排边界：
prepare_receipt 只负责单据级聚合后拆成发票级 preparedInput 列表；
process_prepared_receipt 只负责遍历 invoicePreparations 并调用 runtime。
明确稳定契约。prepared_receipt 继续作为准备阶段产物，包含 receiptCode、receiptContext、serviceData、invoicePreparations、summary；
invoiceResults 继续作为执行阶段产物，但要补齐可入库字段，比如 status、errorMessage、startedAt、finishedAt、attempt。
把发票循环正式留在 process_prepared_receipt。每张发票都走同一条小流程：
取 preparedInput；
调 runtime；
归一化成单张 invoice result；
追加到 invoiceResults；
更新 receipt summary。
不在 worker 里入库，改成应用层回调或 sink。建议在 bootstrap.py 组装一个可注入的结果持久化接口，默认 no-op。
这样每张发票执行完就可以单独落库，不会因为后面某张失败丢掉前面结果。
默认采用“部分成功”策略。推荐语义是：
某张发票业务失败或 runtime 失败，记录该发票失败结果，但继续处理剩余发票；
最终整单 summary 给出 completedCount、failedCount、warningCount 和 overallStatus。
只有 receiptContext 准备失败这种单据级前置错误，才直接整单失败。
worker 保持瘦。 rabbitmq_worker.py 只做：
取消息；
解析 receiptCode；
调 prepare_receipt；
调 process_prepared_receipt；
按整单结果 ack 或 nack。
不要让 worker 自己关心 invoiceKey、preparedInput 拼装或执行顺序。
保持 core.py 纯粹。它只负责：
prepare_receipt_context 做单据级数据聚合；
prepare_invoice_input 做单张发票输入构造。
不要把 runtime 循环或入库逻辑下沉到这里。
先串行执行，但预留执行策略抽象。第一版为了正确性和可观测性，建议串行；
但在应用层把“单张发票执行”包成可替换策略，后面再平滑切到并发。
用现有测试收紧行为边界。重点补 tests/unit/test_main.py、tests/unit/test_rabbitmq_worker.py、tests/unit/test_prepare_from_queue.py：
混合成功/警告/失败的多发票场景；
单张发票异常但整单继续；
每张结果的 sink 回调顺序；
worker 仍然只调用两段式 receipt API。
关键落点

application.py
这里就是发票级循环的控制中心，后续扩展也应继续放这里。
core.py
保持为“准备数据”的组件，不负责执行编排。
rabbitmq_worker.py
保持为“基础设施入口”，不侵入业务循环。
bootstrap.py
适合接入后续的结果持久化 sink、执行策略、监控回调。
设计决策

发票级循环控制点：应用服务层。
默认失败策略：部分成功，单张失败不拖垮整单。
默认入库策略：逐张发票结果回调/落库。
默认执行策略：先串行，再预留并发扩展。
兼容路径：prepare_input 和 evaluate 保留，但仅作为旧的单发票便捷入口，不再承担队列主流程。