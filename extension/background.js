chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'EVAL') {
    // КРИТИЧНО: вернуть true чтобы sendResponse работал асинхронно в MV3
    handleEval(message).then(sendResponse).catch(err => sendResponse({error: err.message}));
    return true; // держит канал открытым
  }
});

async function handleEval(message) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const res = await fetch('https://indmart.ru/api/avito-eval', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      signal: controller.signal,
      body: JSON.stringify({
        title: message.title,
        price: message.price,
        region: message.region,
        description: message.description
      })
    });
    clearTimeout(timeout);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } catch (e) {
    clearTimeout(timeout);
    if (e.name === 'AbortError') return {error: 'Превышено время ожидания'};
    return {error: 'Сервер недоступен: ' + e.message};
  }
}

// Keepalive — не даёт service worker засыпать
chrome.runtime.onConnect.addListener(port => {
  if (port.name === 'keepalive') {
    port.onDisconnect.addListener(() => {});
  }
});
