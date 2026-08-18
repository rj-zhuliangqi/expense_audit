"""
快速验证 RabbitMQ 队列连通性 & 消息投递/消费的一次性脚本。

用法:
    python3 test_rabbitmq_queue.py [check|send|peek]

  check  — 仅检查连接 + 各队列消息数（默认）
  send   — 向 audit_queue 投一条测试消息
  peek   — 取出一条消息只读不消费（requeue 回去）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from expense_audit_orchestrator.paths import PROJECT_ROOT

# 自动加载 .env（如果安装了 python-dotenv）
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

try:
    import pika
except ImportError:
    print("❌  pika 未安装，请先: pip install pika")
    sys.exit(1)

from apps.workers.rabbitmq_worker import RabbitMQSettings, resolve_amqp_url

SETTINGS = RabbitMQSettings(amqp_url=resolve_amqp_url())
AMQP_URL = SETTINGS.amqp_url

QUEUES = [
    SETTINGS.queue_name,
    SETTINGS.monthly_queue_name,
    SETTINGS.delay_process_queue_name,
    SETTINGS.delay_queue_name,
]
TEST_RECEIPT_CODE = "TEST_PEEK_001"


def _connect() -> pika.BlockingConnection:
    print(f"→  连接 {AMQP_URL.split('@')[-1]} …")   # 隐藏密码
    return pika.BlockingConnection(pika.URLParameters(AMQP_URL))


# ──────────────────────────────────────────────
# 1. check — 列出各队列的积压消息数
# ──────────────────────────────────────────────
def cmd_check():
    conn = _connect()
    ch = conn.channel()

    print("\n队列状态：")
    for q in QUEUES:
        try:
            result = ch.queue_declare(queue=q, durable=True, passive=True)
            count = result.method.message_count
            consumer_count = result.method.consumer_count
            status = "✅" if count >= 0 else "?"
            print(f"  {status}  {q:<40}  messages={count}  consumers={consumer_count}")
        except pika.exceptions.ChannelClosedByBroker as e:
            print(f"  ⚠️  {q:<40}  {e}")
            # channel 被关闭后需要重建
            ch = conn.channel()

    conn.close()
    print("\n连通性 OK — RabbitMQ 可以正常访问。")


# ──────────────────────────────────────────────
# 2. send — 向默认主队列路由投一条测试消息
# ──────────────────────────────────────────────
def cmd_send():
    conn = _connect()
    ch = conn.channel()

    import json
    body = json.dumps({"receiptCode": TEST_RECEIPT_CODE})
    ch.basic_publish(
        exchange=SETTINGS.exchange_name,
        routing_key=SETTINGS.routing_key,
        body=body,
        properties=pika.BasicProperties(delivery_mode=2),  # 持久化
    )
    print(f"✅  测试消息已发送: {body}")
    conn.close()


# ──────────────────────────────────────────────
# 3. peek — 取出一条只读不消费
# ──────────────────────────────────────────────
def cmd_peek(queue: str = SETTINGS.queue_name):
    conn = _connect()
    ch = conn.channel()

    method_frame, _header, body = ch.basic_get(queue=queue, auto_ack=False)
    if method_frame:
        print(f"✅  队列 [{queue}] 有消息！")
        print(f"   delivery_tag  : {method_frame.delivery_tag}")
        print(f"   routing_key   : {method_frame.routing_key}")
        print(f"   body          : {body.decode('utf-8', errors='replace')}")
        ch.basic_nack(delivery_tag=method_frame.delivery_tag, requeue=True)
        print("   → 消息已 requeue 回队列，未实际消费。")
    else:
        print(f"⚠️  队列 [{queue}] 当前为空。")

    conn.close()


# ──────────────────────────────────────────────
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    try:
        if cmd == "check":
            cmd_check()
        elif cmd == "send":
            cmd_send()
        elif cmd == "peek":
            target_q = sys.argv[2] if len(sys.argv) > 2 else SETTINGS.queue_name
            cmd_peek(target_q)
        else:
            print(f"未知命令: {cmd}。可用: check | send | peek")
            sys.exit(1)
    except pika.exceptions.AMQPConnectionError as exc:
        print(f"\n❌  无法连接 RabbitMQ: {exc}")
        print("    请检查: URL 是否正确、端口 30116 是否可达、VPN/网络是否通")
        sys.exit(1)


if __name__ == "__main__":
    main()
