const receiptCodeInput = document.querySelector('#receipt-code');
const queryButton = document.querySelector('#query-button');
const copyButton = document.querySelector('#copy-button');
const statusElement = document.querySelector('#status');
const resultCount = document.querySelector('#result-count');
const jsonOutput = document.querySelector('#json-output');

let formattedJson = '';

function setStatus(message, state = '') {
  statusElement.textContent = message;
  statusElement.dataset.state = state;
}

async function queryReceipt() {
  const receiptCode = receiptCodeInput.value.trim();
  if (!receiptCode) {
    setStatus('请输入核销单号。', 'error');
    receiptCodeInput.focus();
    return;
  }

  queryButton.disabled = true;
  copyButton.disabled = true;
  resultCount.textContent = '正在读取...';
  jsonOutput.textContent = '正在读取...';
  setStatus('正在读取 preparedInput...', 'loading');

  try {
    const response = await fetch(`/api/receipts/${encodeURIComponent(receiptCode)}`);
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.detail || '查询失败');
    }

    formattedJson = JSON.stringify(body, null, 2);
    jsonOutput.textContent = formattedJson;
    resultCount.textContent = `${body.length} 张发票的 preparedInput`;
    copyButton.disabled = false;
    setStatus('读取成功。', 'success');
  } catch (error) {
    formattedJson = '';
    jsonOutput.textContent = '暂无结果';
    resultCount.textContent = '未加载数据';
    setStatus(error.message || '查询失败，请稍后重试。', 'error');
  } finally {
    queryButton.disabled = false;
  }
}

async function copyJson() {
  if (!formattedJson) return;

  try {
    await navigator.clipboard.writeText(formattedJson);
    setStatus('JSON 已复制。', 'success');
  } catch {
    setStatus('复制失败，请检查浏览器剪贴板权限。', 'error');
  }
}

queryButton.addEventListener('click', queryReceipt);
copyButton.addEventListener('click', copyJson);
receiptCodeInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') queryReceipt();
});
