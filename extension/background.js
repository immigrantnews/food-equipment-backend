chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'EVAL') {
    // КРИТИЧНО: вернуть true чтобы sendResponse работал асинхронно в MV3
    handleEval(message).then(sendResponse).catch(err => sendResponse({error: err.message}));
    return true; // держит канал открытым
  }

  if (message.type === 'NOTIFY') {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icon48.png',
      title: message.title,
      message: message.body,
      buttons: [{title: 'Открыть объявление'}],
      requireInteraction: true
    });
    // Сохраняем URL для открытия при клике
    chrome.storage.local.set({last_notification_url: message.url});
    sendResponse({ok: true});
    return true;
  }
});

// Открываем объявление при клике на уведомление
chrome.notifications.onButtonClicked.addListener((notificationId, buttonIndex) => {
  chrome.storage.local.get('last_notification_url', (data) => {
    if (data.last_notification_url) {
      chrome.tabs.create({url: data.last_notification_url});
    }
  });
});

chrome.notifications.onClicked.addListener((notificationId) => {
  chrome.storage.local.get('last_notification_url', (data) => {
    if (data.last_notification_url) {
      chrome.tabs.create({url: data.last_notification_url});
    }
  });
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
