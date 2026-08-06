# Prepared Receipt Viewer

这是一个只用于测试的独立工具，不会注册到主项目路由，也不会被 `start_all_services.sh` 或 systemd 启动。

## 功能

输入核销单号后，工具读取：

```text
output/worker-debug/prepared/<核销单号>.prepared-receipt.json
```

接口只返回每个 `invoicePreparations[*].preparedInput` 的值，返回格式是纯数组：

```json
[
  {"invoiceNo": "..."},
  {"invoiceNo": "..."}
]
```

页面支持格式化展示和复制 JSON。

## 启动

在仓库根目录执行下面的局域网启动脚本：

```bash
./tools/prepared_receipt_viewer/run_lan.sh
```

脚本默认监听 `0.0.0.0:8092`。在运行服务的机器上查看局域网 IP：

```bash
hostname -I
```

然后在同一局域网的其他电脑或手机浏览器访问：

```text
http://<运行服务机器的局域网IP>:8092/
```

例如：`http://192.168.1.20:8092/`。

如果访问不了，请检查运行服务机器的防火墙是否放行 TCP `8092` 端口，以及客户端和服务端是否处于同一局域网。该工具会读取发票和企业信息，建议只在可信局域网使用，不要直接暴露到公网。

## 数据目录覆盖

可以通过 `PREPARED_RECEIPT_DIR` 指定其他测试数据目录：

```bash
PREPARED_RECEIPT_DIR=/tmp/prepared \
./tools/prepared_receipt_viewer/run_lan.sh
```

## 测试

```bash
.venv/bin/python -m unittest tools.prepared_receipt_viewer.test_app -v
```
