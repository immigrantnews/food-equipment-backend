(function () {
  const KEYWORDS = ["тестомес","расстойк","тестораскаточ","делитель","миксер планетарн","хлебопекарн","пекарн","дежа","ротационн","подовая","конвекционн","просеиватель","слайсер","мясорубк","фритюрниц","пароконвектомат","холодильн витрин","шкаф расстойн","тестомесильн","взбивалк","куттер","вакуумн упаковщ","жарочн шкаф","печь","тестоделитель","округлитель","котел пищевой","картофелечистк","овощерезк","коптильн","льдогенератор","раскаточн","раскатк","ламинатор теста","тестовалк","тестомешалк","тестоотсадочн","тестозакаточн","круассаномат","закаточн машин","отсадочн","формовочн","инфел","foodatlas","шфз","мфп","варочн котел","пищевар","плита промышл","дымогенератор","ледогенератор","витрин холодильн","лари морозильн","шкаф холодильн","холодильн камер","мешалка","лаваш","хлеб","eksi","ehtd","производство хлеб","хлебопроизводств","тестомесительн","спиральн мешалк","планетарн мешалк","миксер спиральн"];

  function hasKeywords(text) {
    const lower = text.toLowerCase();
    return KEYWORDS.some(kw =>
      kw.length <= 4
        ? new RegExp('(^|[\\s,.:;!?\\-\\/\\\\(\\)])' + kw + '($|[\\s,.:;!?\\-\\/\\\\(\\)])').test(lower)
        : lower.includes(kw)
    );
  }

  // Кеш статистики цен
  let priceStats = null;
  let priceStatsLoaded = false;

  async function loadPriceStats() {
    if (priceStatsLoaded) return;
    try {
      const resp = await fetch('https://indmart.ru/api/price-stats');
      priceStats = await resp.json();
      priceStatsLoaded = true;
    } catch(e) {
      priceStatsLoaded = true; // не повторяем при ошибке
    }
  }

  function getVerdictFromStats(title, price) {
    if (!priceStats || !price) return null;
    const titleLower = title.toLowerCase();

    for (const [key, stats] of Object.entries(priceStats)) {
      const parts = key.split('/');
      const category = parts[0];
      const brand = parts[1];
      const model = parts[2];

      // Проверяем совпадение по бренду или модели
      if ((brand && titleLower.includes(brand)) ||
          (model && titleLower.includes(model)) ||
          (category && titleLower.includes(category))) {
        const median = stats.median;
        const ratio = price / median;
        if (ratio < 0.65) return {verdict: 'flash', label: '⚡', median};
        if (ratio < 0.85) return {verdict: 'green', label: '🟢', median};
        if (ratio < 1.15) return {verdict: 'yellow', label: '🟡', median};
        return {verdict: 'red', label: '🔴', median};
      }
    }
    return null;
  }

  function addCatalogBadges() {
    if (!priceStats) return;

    // Находим карточки в каталоге
    const items = document.querySelectorAll('[data-marker="item"]');
    items.forEach(item => {
      if (item.querySelector('.indmart-badge-mini')) return; // уже добавили

      const titleEl = item.querySelector('[itemprop="name"], [data-marker="item-title"]');
      const priceEl = item.querySelector('[itemprop="price"], [data-marker="item-price"]');

      if (!titleEl || !priceEl) return;

      const title = titleEl.textContent.trim();
      const price = parseInt((priceEl.getAttribute('content') || priceEl.textContent).replace(/\D/g,''));

      if (!price || !hasKeywords(title)) return;

      const result = getVerdictFromStats(title, price);
      if (!result) return;

      const colors = {flash:'#8b5cf6', green:'#22c55e', yellow:'#f59e0b', red:'#ef4444'};
      const badge = document.createElement('span');
      badge.className = 'indmart-badge-mini';
      badge.title = `IndMart: медиана ${result.median.toLocaleString('ru')} ₽`;
      badge.style.cssText = `display:inline-block;background:${colors[result.verdict]};color:#fff;border-radius:4px;padding:2px 6px;font-size:11px;font-weight:700;margin-left:6px;vertical-align:middle`;
      badge.textContent = result.label + ' IndMart';

      titleEl.appendChild(badge);
    });
  }

  function getText(sels) {
    for (const s of sels) {
      const el = document.querySelector(s);
      if (el && (el.getAttribute('content') || el.textContent || '').trim())
        return (el.getAttribute('content') || el.textContent).trim();
    }
    return '';
  }
  function getSite() {
    const host = location.hostname;
    if (host.includes('avito')) return 'avito';
    if (host.includes('youla')) return 'youla';
    if (host.includes('agroserver')) return 'agroserver';
    return 'other';
  }
  function parseTitle() {
    if (getSite() === 'youla') {
      const h2 = document.querySelector('h2');
      if (h2?.textContent?.trim()) return h2.textContent.trim();
    }
    if (getSite() === 'agroserver') {
      const agroSelectors = [
        'h1.product-title',
        'h1.title',
        '.product-name h1',
        '.card-title',
        'h1'
      ];
      for (const sel of agroSelectors) {
        const el = document.querySelector(sel);
        if (el?.textContent?.trim()) return el.textContent.trim();
      }
    }
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
  function getPrice() {
    if (getSite() === 'youla') {
      const h3 = document.querySelector('h3');
      if (h3) {
        const num = parseInt(h3.textContent.replace(/\D/g, ''));
        if (num > 0) return num;
      }
    }
    if (getSite() === 'agroserver') {
      const raw = getText(['.mprice','.price','[class*="price"]']);
      const num = parseInt(raw.replace(/\D/g, ''));
      if (num > 0) return num;
    }
    const raw = getText(['[itemprop="price"]','[data-marker="item-view/item-price"]','[class*="price-value"]','[class*="item-price"]','[data-test="product-price"]','[class*="ProductPrice"]']);
    return parseInt(raw.replace(/\D/g, '')) || 0;
  }
  function getRegion() {
    if (getSite() === 'agroserver') {
      return getText(['.bl.ico_location', '[class*="ico_location"]', '[class*="location"]']).slice(0, 100);
    }
    return getText(['[data-marker="item-address/name"]','[class*="address"]','[data-test="product-location"]','[class*="ProductLocation"]']);
  }
  function getDescription() {
    if (getSite() === 'agroserver') {
      return getText(['.product-description','.description','[class*="description"]','.product-text']).slice(0, 300);
    }
    return getText(['[data-marker="item-view/item-description"]','[itemprop="description"]','[data-test="product-description"]','[class*="ProductDescription"]']).slice(0, 300);
  }
  async function makeButton() {
    if (document.getElementById('indmart-btn') || document.getElementById('indmart-widget')) return;

    const url = location.href;
    const isEquipment =
      url.includes('oborudovanie') ||
      url.includes('pishchevoe') ||
      url.includes('tovary_dlya_biznesa') ||
      url.includes('selskoe_hozyaystvo') ||
      url.includes('promyshlennoe') ||
      url.includes('horeca') ||
      url.includes('youla.ru') ||
      url.includes('agroserver.ru');  // accept all youla/agroserver pages, filter by keywords
    if (!isEquipment) return;

    const title = parseTitle();
    const price = getPrice();
    if (!title || !price) return;
    if (!hasKeywords(title)) return;

    // Проверяем режим — в режиме покупателя кнопку не показываем пока не готово
    const userMode = await new Promise(resolve =>
      chrome.storage.sync.get('userMode', ({userMode}) => resolve(userMode || 'reseller'))
    );
    if (userMode === 'buyer') return;

    const btn = document.createElement('div');
    btn.id = 'indmart-btn';
    btn.textContent = '📊 IndMart: оценить';
    btn.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:2147483647;background:#1a1a2e;color:#fff;padding:14px 20px;border-radius:12px;font-family:system-ui,sans-serif;font-size:15px;font-weight:600;cursor:pointer;box-shadow:0 4px 20px rgba(0,0,0,0.25)';
    btn.onclick = () => evaluate(title, price);
    document.body.appendChild(btn);
  }
  function getSellerType() {
    // Youla: явный признак типа продавца
    const youlaSeller = document.querySelector('[data-test="seller-type"]');
    if (youlaSeller) {
      const t = youlaSeller.textContent.toLowerCase();
      if (t.includes('компания') || t.includes('магазин')) return 'company';
      return 'private';
    }

    // Проверка отображаемого имени продавца на бизнес-признаки
    // (выполняется до loop, иначе return 'private' внутри loop перехватит управление)
    const sellerNameEl = document.querySelector('[data-marker="seller-info/name"]') ||
                         document.querySelector('[class*="seller-name"]') ||
                         document.querySelector('[class*="style-seller-info"] a');
    if (sellerNameEl) {
      const sellerName = sellerNameEl.textContent.toLowerCase();
      const businessNameKeywords = [
        'центр', 'сервис', 'торг', 'маркет', 'снаб', 'техник',
        'оборудован', 'групп', 'холдинг', 'предприяти', 'завод',
        'производств', 'поставк', 'опт', 'магазин', 'склад',
        'восстановлен', 'ремонт', 'обслуживан'
      ];
      if (businessNameKeywords.some(kw => sellerName.includes(kw))) {
        return 'company';
      }
    }

    const selectors = [
      '[data-marker="seller-info/name"]',
      '[data-marker="item-view/item-seller"]',
      '[class*="seller-info"]',
      '[class*="style-seller-info"]'
    ];

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const raw = el.textContent;
      const text = raw.toLowerCase();
      if (text.includes('компания') || text.includes('магазин') ||
          text.includes('официальный') || text.includes('дилер') ||
          text.includes('ооо') || text.includes('ип ') ||
          text.includes('центр') || text.includes('восстановлен') ||
          /[A-ZА-Я]{3,}/.test(raw)) {
        return 'company';
      }
      return 'private';
    }

    // Метка "Компания" рядом с блоком информации о продавце
    const sellerSection = document.querySelector('[data-marker="seller-info"]') ||
                          document.querySelector('[class*="seller-info"]');
    if (sellerSection) {
      const sectionText = sellerSection.textContent.toLowerCase();
      if (sectionText.includes('компания')) return 'company';
    }

    // Проверяем по наличию "Компания" на странице
    const allText = document.body.textContent.toLowerCase();
    if (allText.includes('\nкомпания\n') || allText.includes('компания\n')) {
      return 'company';
    }
    return 'unknown';
  }

  async function evaluate(title, price) {
    const btn = document.getElementById('indmart-btn');
    if (btn && btn.dataset.loading === '1') return;

    // Сначала кеш — показываем результат без запроса к API
    const cachedResult = await loadFromCache(location.href);
    if (cachedResult) {
      showWidget(cachedResult, cachedResult._price || price, cachedResult._title || title, cachedResult._sellerType || 'unknown', cachedResult._userMode || 'reseller');
      return;
    }

    if (btn) { btn.textContent = '⏳ Анализирую...'; btn.dataset.loading = '1'; }

    const region = getRegion();
    const description = getDescription();
    const sellerType = getSellerType();
    const userMode = await new Promise(resolve =>
      chrome.storage.sync.get('userMode', ({userMode}) => resolve(userMode || 'reseller'))
    );

    // Берём фото больше 200px шириной (не миниатюры)
    let photoUrls;
    if (location.href.includes('youla.ru')) {
      // У Youla другой CDN — фильтр avito.st не подходит, берём из галереи
      const galleryImgs = document.querySelectorAll('[class*="PhotoGallery"] img, [data-test="gallery-image"]');
      photoUrls = Array.from(galleryImgs)
        .map(el => el.src || el.getAttribute('data-src') || '')
        .filter(src => src.startsWith('http'))
        .slice(0, 3);
    } else {
      const allImgs = document.querySelectorAll('img');
      photoUrls = Array.from(allImgs)
        .filter(el => el.src && el.src.includes('avito.st') && el.width > 200)
        .slice(0, 3)
        .map(el => el.src)
        .filter(Boolean);
    }

    const photos = [];
    for (const url of photoUrls) {
      try {
        const resp = await fetch(url);
        const blob = await resp.blob();
        // Сжимаем через canvas до ширины 800px
        const compressed = await compressImage(blob);
        photos.push(compressed);
      } catch (e) {}
    }

    try {
      const resp = await fetch('https://indmart.ru/api/avito-eval', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({title, price, region, description, photos, seller_type: sellerType, user_mode: userMode, listing_url: location.href})
      });
      const d = await resp.json();
      d._price = price;
      d._title = title;
      d._sellerType = sellerType;
      d._userMode = userMode;
      saveToCache(location.href, d);
      showWidget(d, price, title, sellerType, userMode);
    } catch (e) {
      if (btn) { btn.textContent = '❌ Ошибка'; btn.dataset.loading = '0'; }
    }
  }

  // Сжатие картинки до ширины 800px и конвертация в base64 jpeg
  function compressImage(blob) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      const url = URL.createObjectURL(blob);
      img.onload = () => {
        URL.revokeObjectURL(url);
        const maxW = 800;
        const scale = Math.min(1, maxW / img.width);
        const canvas = document.createElement('canvas');
        canvas.width = img.width * scale;
        canvas.height = img.height * scale;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
        // отдаём только base64 часть без префикса
        resolve(dataUrl.split(',')[1]);
      };
      img.onerror = reject;
      img.src = url;
    });
  }

  // Персистентный кеш оценок в chrome.storage.local (24 часа, до 50 записей)
  async function saveToCache(url, data) {
    try {
      const cache = await new Promise(resolve =>
        chrome.storage.local.get('eval_cache', d => resolve(d.eval_cache || {}))
      );
      cache[url] = {data, ts: Date.now()};
      const keys = Object.keys(cache);
      if (keys.length > 50) {
        const oldest = keys.sort((a,b) => cache[a].ts - cache[b].ts)[0];
        delete cache[oldest];
      }
      chrome.storage.local.set({eval_cache: cache});
    } catch(e) {}
  }

  async function loadFromCache(url) {
    try {
      const cache = await new Promise(resolve =>
        chrome.storage.local.get('eval_cache', d => resolve(d.eval_cache || {}))
      );
      const entry = cache[url];
      if (!entry) return null;
      if (Date.now() - entry.ts > 24 * 60 * 60 * 1000) return null;
      return entry.data;
    } catch(e) { return null; }
  }

  function showWidget(d, price, title, sellerType, userMode) {
    document.getElementById('indmart-btn')?.remove();
    document.getElementById('indmart-widget')?.remove();
    if (!document.getElementById('indmart-style')) {
      const st = document.createElement('style');
      st.id = 'indmart-style';
      st.textContent = '@keyframes indmart-blink{0%,100%{border-color:#ef4444}50%{border-color:transparent}}';
      document.head.appendChild(st);
    }
    const verdicts = {flash:['#8b5cf6','⚡ СРОЧНО БЕРИТЕ'],green:['#22c55e','🟢 ХОРОШАЯ ЦЕНА'],yellow:['#f59e0b','🟡 В РЫНКЕ'],red:['#ef4444','🔴 ЗАВЫШЕНО'],new_item:['#64748b','🏭 НОВОЕ ОТ ДИЛЕРА']};
    const v = verdicts[d.verdict] || verdicts.yellow;
    const name = [d.category, d.brand, d.model].filter(Boolean).join(' ');
    const demand = {high:'высокий',medium:'средний',low:'низкий'}[d.demand] || d.demand;
    const urgent = d.urgency === 'urgent' || d.urgency === 'liquidation';
    const condLabels = {
      poor: '⚠️ Состояние: плохое — учтите расходы на ремонт',
      fair: '⚠️ Состояние: требует ТО',
      good: '✅ Состояние: хорошее',
      excellent: '✅ Состояние: отличное'
    };
    const condLine = condLabels[d.condition_visual] ? '<br>'+condLabels[d.condition_visual] : '';

    let banners = '';
    if (urgent)
      banners += '<div style="background:#ef4444;color:#fff;padding:8px 16px;text-align:center;font-weight:700">⚡ СРОЧНАЯ ПРОДАЖА — торгуйтесь!</div>';
    if (d.bulk_opportunity === true)
      banners += '<div style="background:#0ea5e9;color:#fff;padding:8px 16px;text-align:center;font-weight:700">📦 ОПТОВЫЙ ЛОТ — можно взять всё дешевле</div>';
    if (d.verdict === 'new_item')
      banners += '<div style="background:#64748b;color:#fff;padding:8px 16px;text-align:center;font-weight:600">🏭 Новое от дилера — перекупщику неинтересно</div>';
    if (d.notification_reason)
      banners += '<div style="background:#fef3c7;color:#92400e;padding:10px 16px;font-weight:600;border-left:4px solid #f59e0b">'+d.notification_reason+'</div>';

    const w = document.createElement('div');
    w.id = 'indmart-widget';
    w.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:2147483647;width:300px;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,0.18);font-family:system-ui,sans-serif;font-size:14px;background:#fff;overflow:hidden' +
      (urgent ? ';border:3px solid #ef4444;animation:indmart-blink 1s infinite' : '');
    w.innerHTML =
      '<div style="background:#1a1a2e;color:#fff;padding:12px 16px;display:flex;justify-content:space-between;align-items:center"><span><b>IndMart</b><span style="font-size:10px;background:rgba(255,255,255,0.2);padding:2px 6px;border-radius:4px;margin-left:6px">'+(userMode === 'buyer' ? '🏭 Покупатель' : '💼 Перекупщик')+'</span></span><span class="indmart-close" style="cursor:pointer">✕</span></div>' +
      (sellerType === 'private' ? '<div style="background:#dcfce7;color:#166534;padding:8px 16px;text-align:center;font-weight:600">👤 Частное лицо — торгуйтесь!</div>' : '') +
      (sellerType === 'company' ? '<div style="background:#f1f5f9;color:#475569;padding:8px 16px;text-align:center;font-weight:600">🏢 Компания — цена фиксированная</div>' : '') +
      '<div style="background:'+v[0]+';color:#fff;padding:10px;text-align:center;font-weight:700">'+v[1]+'</div>' +
      banners +
      '<div style="padding:14px 16px">' +
      '<div style="font-weight:600;margin-bottom:8px">'+name+'</div>' +
      '<div style="color:#444;margin-bottom:4px">Рынок: <b>'+d.market_min.toLocaleString('ru')+' – '+d.market_max.toLocaleString('ru')+' ₽</b></div>' +
      '<div style="background:#f5f5f5;border-radius:8px;padding:10px;margin:10px 0;line-height:1.8">💰 Маржа: <b>'+d.reseller_margin.toLocaleString('ru')+' ₽</b><br>⏱ Оборот: <b>'+d.turnover_days+'</b><br>📊 Спрос: <b>'+demand+'</b>'+condLine+'</div>' +
      '<div style="font-style:italic;color:#666;font-size:13px;margin-bottom:12px">"'+d.comment+'"</div>' +
      '<a href="https://t.me/IndMartAlertsBot?start=subscribe" target="_blank" style="display:block;background:#1a1a2e;color:#fff;text-align:center;padding:11px;border-radius:8px;text-decoration:none;font-weight:600">🔔 Подписаться в Telegram</a>' +
      '<a href="https://indmart.ru" target="_blank" style="display:block;color:#888;text-align:center;padding:8px 0 0;text-decoration:none;font-size:12px">Открыть IndMart</a>' +
      '</div>';
    if (d.urgency !== 'normal' || d.bulk_opportunity) {
      sendNotification(d, title || '', price || 0);
    }
    document.body.appendChild(w);

    const closeBtn = w.querySelector('.indmart-close');
    if (closeBtn) {
      closeBtn.addEventListener('click', () => {
        w.remove();
        // кнопку не трогаем — setInterval вернёт её, клик покажет результат из кеша
      });
    }

    checkFilters(d, sellerType || 'unknown').then(({matches, reason}) => {
      const widget = document.getElementById('indmart-widget');
      if (!widget) return;

      if (matches && (d.urgency !== 'normal' || d.bulk_opportunity)) {
        widget.style.border = '3px solid #22c55e';
        // Add animation style only once
        if (!document.getElementById('indmart-pulse-style')) {
          const style = document.createElement('style');
          style.id = 'indmart-pulse-style';
          style.textContent = '@keyframes indmart-pulse{0%,100%{box-shadow:0 8px 32px rgba(34,197,94,0.3)}50%{box-shadow:0 8px 32px rgba(34,197,94,0.8)}}';
          document.head.appendChild(style);
        }
        widget.style.animation = 'indmart-pulse 1s ease-in-out 3';
        const matchBadge = document.createElement('div');
        matchBadge.style.cssText = 'background:#22c55e;color:#fff;text-align:center;padding:6px;font-weight:700;font-size:13px;';
        matchBadge.textContent = '✅ Соответствует вашим критериям!';
        widget.insertBefore(matchBadge, widget.firstChild);
      } else if (!matches && reason) {
        const mismatch = document.createElement('div');
        mismatch.style.cssText = 'background:#f5f5f5;color:#888;text-align:center;padding:6px;font-size:12px;border-bottom:1px solid #eee;';
        mismatch.textContent = '⚠️ Не соответствует: ' + reason;
        const body = widget.querySelector('.indmart-body');
        widget.insertBefore(mismatch, body || widget.firstChild);
      }
    });
  }

  function parseTurnoverMax(turnoverStr) {
    if (!turnoverStr) return 999;
    const nums = turnoverStr.match(/\d+/g);
    if (!nums) return 999;
    return Math.max(...nums.map(Number));
  }

  async function checkFilters(data, sellerType) {
    return new Promise(resolve => {
      chrome.storage.sync.get('filters', ({filters}) => {
        if (!filters) return resolve({matches: true, reason: ''});

        const issues = [];

        if (filters.minMargin > 0 && data.reseller_margin < filters.minMargin) {
          issues.push(`маржа ${data.reseller_margin.toLocaleString('ru')} ₽ < минимума ${filters.minMargin.toLocaleString('ru')} ₽`);
        }

        if (filters.maxTurnover > 0) {
          const maxDays = parseTurnoverMax(data.turnover_days);
          if (maxDays > filters.maxTurnover) {
            issues.push(`оборот ${data.turnover_days} > ${filters.maxTurnover} дней`);
          }
        }

        if (filters.privateOnly && sellerType === 'company') {
          issues.push('продавец — компания');
        }

        if (filters.urgentOnly && data.urgency === 'normal') {
          issues.push('не срочная продажа');
        }

        resolve({
          matches: issues.length === 0,
          reason: issues.join(', ')
        });
      });
    });
  }

  function sendNotification(data, title, price) {
    // Отправляем уведомление только для срочных и оптовых
    if (data.urgency === 'normal' && !data.bulk_opportunity) return;

    // Проверяем не отправляли ли уже для этого URL
    const url = location.href;
    chrome.storage.local.get('notified_urls', (s) => {
      const notified = s.notified_urls || {};
      if (notified[url]) return;

      let notifTitle = '';
      let notifBody = '';

      if (data.urgency === 'liquidation') {
        notifTitle = '🔥 ЛИКВИДАЦИЯ — срочная продажа!';
        notifBody = `${title} за ${price.toLocaleString('ru')} ₽ — ${data.comment}`;
      } else if (data.urgency === 'urgent') {
        notifTitle = '⚡ Срочная продажа';
        notifBody = `${title} за ${price.toLocaleString('ru')} ₽ — торгуйтесь!`;
      } else if (data.bulk_opportunity) {
        notifTitle = '📦 Оптовый лот';
        notifBody = `${title} — ${data.notification_reason || 'можно взять всё дешевле'}`;
      }

      if (!notifTitle) return;

      chrome.runtime.sendMessage({
        type: 'NOTIFY',
        title: notifTitle,
        body: notifBody,
        url: url
      });

      // Помечаем URL как уже уведомлённый
      notified[url] = Date.now();
      chrome.storage.local.set({notified_urls: notified});
    });
  }
  let lastUrl = '';
  setInterval(async () => {
    // Загружаем статистику цен при первом запуске
    await loadPriceStats();

    if (location.href !== lastUrl) {
      lastUrl = location.href;
      document.getElementById('indmart-btn')?.remove();
      document.getElementById('indmart-widget')?.remove();
    }

    // Добавляем метки в каталог
    const isCatalog = document.querySelector('[data-marker="catalog-serp"]') ||
                      document.querySelector('[data-marker="search-results"]');
    if (isCatalog) {
      addCatalogBadges();
    } else {
      makeButton();
    }
  }, 1000);
  console.log('IndMart loaded');
})();
