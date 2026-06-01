// Кеш оценённых страниц
const evaluated = new Map();
let debounceTimer = null;
let currentObserver = null;

// Keepalive — будим service worker перед запросом
function wakeUpServiceWorker() {
  try {
    const port = chrome.runtime.connect({name: 'keepalive'});
    setTimeout(() => port.disconnect(), 500);
  } catch(e) {}
}

// Проверка что это страница товара
function isItemPage() {
  return location.href.includes('/items/') ||
         (document.querySelector('h1') && document.querySelector('[class*="price"]'));
}

// Парсинг цены — несколько fallback селекторов
function parsePrice() {
  const selectors = [
    '[itemprop="price"]',
    '[data-marker="item-view/item-price"]',
    '.price-value-main',
    '[class*="price-value"]',
    '[class*="item-price"]'
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const raw = el.getAttribute('content') || el.textContent;
    const num = parseInt(raw.replace(/\D/g, ''));
    if (num > 0) return num;
  }
  return 0;
}

// Парсинг заголовка
function parseTitle() {
  const selectors = [
    'h1[itemprop="name"]',
    '[data-marker="item-view/title"]',
    'h1'
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el?.textContent?.trim()) return el.textContent.trim();
  }
  return '';
}

// Парсинг региона
function parseRegion() {
  const selectors = [
    '[data-marker="item-address/name"]',
    '[class*="address-root"]',
    '[class*="geo-root"]',
    '[class*="location"]'
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el?.textContent?.trim()) return el.textContent.trim().slice(0, 100);
  }
  return '';
}

// Парсинг описания
function parseDescription() {
  const selectors = [
    '[data-marker="item-view/item-description"]',
    '[itemprop="description"]',
    '[class*="description"]'
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el?.textContent?.trim()) return el.textContent.trim().slice(0, 300);
  }
  return '';
}

// Ключевые слова пищевого оборудования
const KEYWORDS = [
  "тестомес", "расстойк", "тестораскаточ", "делитель",
  "миксер планетарн", "хлебопекарн", "пекарн", "дежа", "ротационн",
  "подовая", "конвекционн", "просеиватель", "слайсер", "мясорубк",
  "фритюрниц", "пароконвектомат", "холодильн витрин", "шкаф расстойн",
  "тестомесильн", "взбивалк", "куттер", "вакуумн упаковщ",
  "жарочн шкаф", "пищевое оборудование", "хлебопечь"
];

function hasKeywords(text) {
  const lower = text.toLowerCase();
  return KEYWORDS.some(kw => lower.includes(kw));
}

// Счётчик дневного лимита
async function checkAndIncrementLimit() {
  const today = new Date().toDateString();
  return new Promise(resolve => {
    chrome.storage.local.get(['limitDate', 'limitCount'], data => {
      if (data.limitDate !== today) {
        chrome.storage.local.set({limitDate: today, limitCount: 1});
        resolve(true);
      } else if (data.limitCount < 10) {
        chrome.storage.local.set({limitCount: data.limitCount + 1});
        resolve(true);
      } else {
        resolve(false);
      }
    });
  });
}

// Создание лоадера
function showLoader() {
  removeWidget();
  const loader = document.createElement('div');
  loader.id = 'indmart-loader';
  loader.className = 'indmart-loader';
  loader.innerHTML = '<div class="indmart-dots"><span></span><span></span><span></span></div><div class="indmart-loader-text">IndMart анализирует...</div>';
  document.body.appendChild(loader);
}

function removeWidget() {
  document.getElementById('indmart-widget')?.remove();
  document.getElementById('indmart-loader')?.remove();
}

// Расчёт отклонения от середины диапазона
function calcDeviation(price, min, max) {
  const median = (min + max) / 2;
  const pct = Math.round((price - median) / median * 100);
  return pct > 0 ? '+' + pct + '%' : pct + '%';
}

// Перевод demand
function demandLabel(d) {
  return {high: 'высокий', medium: 'средний', low: 'низкий'}[d] || d;
}

// Создание виджета
function showWidget(data) {
  removeWidget();

  if (data.error) {
    // показать ошибку
    const el = document.createElement('div');
    el.id = 'indmart-widget';
    el.className = 'indmart-widget';
    el.innerHTML = `
      <div class="indmart-header">
        <span class="indmart-logo">IndMart</span>
        <button class="indmart-close" onclick="this.closest('#indmart-widget').remove()">×</button>
      </div>
      <div class="indmart-body" style="padding:16px;text-align:center;color:#666">
        ${data.error}
      </div>`;
    document.body.appendChild(el);
    return;
  }

  const verdictMap = {
    flash: {bg: '#8b5cf6', label: '⚡ СРОЧНО БЕРИТЕ'},
    green: {bg: '#22c55e', label: '🟢 ХОРОШАЯ ЦЕНА'},
    yellow: {bg: '#f59e0b', label: '🟡 В РЫНКЕ'},
    red:   {bg: '#ef4444', label: '🔴 ЗАВЫШЕНО'}
  };
  const v = verdictMap[data.verdict] || verdictMap.yellow;
  const deviation = calcDeviation(data.price_asked || 0, data.market_min, data.market_max);
  const equipName = [data.category, data.brand, data.model].filter(Boolean).join(' ');

  const el = document.createElement('div');
  el.id = 'indmart-widget';
  el.className = 'indmart-widget';
  el.innerHTML = `
    <div class="indmart-header">
      <span class="indmart-logo">IndMart</span>
      <button class="indmart-close" onclick="this.closest('#indmart-widget').remove()">×</button>
    </div>
    <div class="indmart-badge" style="background:${v.bg}">${v.label}</div>
    <div class="indmart-body">
      <div class="indmart-name">${equipName}</div>
      <div class="indmart-row">Рынок: <b>${data.market_min.toLocaleString('ru')} – ${data.market_max.toLocaleString('ru')} ₽</b></div>
      <div class="indmart-row">Отклонение: <b>${deviation}</b></div>
      <div class="indmart-reseller">
        <div>💰 Маржа: <b>${data.reseller_margin.toLocaleString('ru')} ₽</b></div>
        <div>⏱ Оборот: <b>${data.turnover_days}</b></div>
        <div>📊 Спрос: <b>${demandLabel(data.demand)}</b></div>
      </div>
      <div class="indmart-comment">"${data.comment}"</div>
      <a class="indmart-btn" href="https://indmart.ru" target="_blank">Открыть в IndMart</a>
    </div>`;
  document.body.appendChild(el);
}

// Главная функция оценки
async function evaluatePage() {
  if (!isItemPage()) return;

  if (document.querySelector('[data-marker="catalog-serp"]') ||
      document.querySelector('[data-marker="search-results"]') ||
      document.querySelectorAll('[data-marker="item"]').length > 1) {
    return;
  }

  const url = location.href;
  if (evaluated.has(url)) {
    showWidget(evaluated.get(url));
    return;
  }

  const title = parseTitle();
  const price = parsePrice();
  const region = parseRegion();
  const description = parseDescription();

  if (!title || price <= 0) return;
  if (!hasKeywords(title + ' ' + description)) return;

  const allowed = await checkAndIncrementLimit();
  if (!allowed) {
    showWidget({error: 'Дневной лимит 10 оценок исчерпан.<br>Зарегистрируйтесь на <a href="https://indmart.ru" target="_blank">indmart.ru</a>'});
    return;
  }

  showLoader();
  wakeUpServiceWorker();

  chrome.runtime.sendMessage({type: 'EVAL', title, price, region, description}, response => {
    if (response) {
      response.price_asked = price;
      evaluated.set(url, response);
      showWidget(response);
    } else {
      removeWidget();
    }
  });
}

// Поллинг — следим за сменой URL и пытаемся оценить страницу
let lastUrl = location.href;

function tryEvaluate() {
  evaluatePage();
}

setInterval(() => {
  if (location.href !== lastUrl) {
    lastUrl = location.href;
    evaluated.delete(lastUrl);
  }
}, 800);

setInterval(tryEvaluate, 500);
