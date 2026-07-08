核销单编号：{{receiptCode}}
发票总数：{{invoiceCount}}

以下是该核销单各发票的稽核问题摘要（仅含 reject/failed/warning，已通过的发票已省略）：

{{problemsDigest}}

请基于上述问题，给出该核销单的整体整改建议。严格输出 JSON：{"aiAuditAdvice": "..."}
