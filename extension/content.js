(function () {
  const KEYWORDS = ["тестомес","расстойк","тестораскаточ","делитель","миксер планетарн","хлебопекарн","пекарн","дежа","ротационн","подовая","конвекционн","просеиватель","слайсер","мясорубк","фритюрниц","пароконвектомат","холодильн витрин","шкаф расстойн","тестомесильн","взбивалк","куттер","вакуумн упаковщ","жарочн шкаф","печь","тестоделитель","округлитель","котел пищевой","картофелечистк","овощерезк","коптильн","льдогенератор"];

  function getText(sels) {
    for (const s of sels) {
      const el = document.querySelector(s);
      if (el && (el.getAttribute('content') || el.textContent || '').trim())
        return (el.getAttribute('content') || el.textContent).trim();
    }
    return '';
  }
  function getPrice() {
    const raw = getText(['[itemprop="price"]','[data-marker="item-view/item-price"]','[class*="price-value"]','[class*="item-price"]']);
    return parseInt(raw.replace(/\D/g, '')) || 0;
  }
  function makeButton() {
    if (document.getElementById('indmart-btn') || document.getElementById('indmart-widget')) return;

    const url = location.href;
    const isEquipment =
      url.includes('oborudovanie') ||
      url.includes('pishchevoe') ||
      url.includes('tovary_dlya_biznesa') ||
      url.includes('selskoe_hozyaystvo') ||
      url.includes('promyshlennoe') ||
      url.includes('horeca');
    if (!isEquipment) return;

    const title = getText(['h1[itemprop="name"]','[data-marker="item-view/title"]','h1']);
    const price = getPrice();
    if (!title || !price) return;
    if (!KEYWORDS.some(k => title.toLowerCase().includes(k))) return;

    const btn = document.createElement('div');
    btn.id = 'indmart-btn';
    btn.textContent = '📊 IndMart: оценить';
    btn.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:2147483647;background:#1a1a2e;color:#fff;padding:14px 20px;border-radius:12px;font-family:system-ui,sans-serif;font-size:15px;font-weight:600;cursor:pointer;box-shadow:0 4px 20px rgba(0,0,0,0.25)';
    btn.onclick = () => evaluate(title, price);
    document.body.appendChild(btn);
  }
  async function evaluate(title, price) {
    const btn = document.getElementById('indmart-btn');
    if (btn && btn.dataset.loading === '1') return;
    if (btn) { btn.textContent = '⏳ Анализирую...'; btn.dataset.loading = '1'; }

    const region = getText(['[data-marker="item-address/name"]','[class*="address"]']);
    const description = getText(['[data-marker="item-view/item-description"]','[itemprop="description"]']).slice(0,300);

    // Берём фото больше 200px шириной (не миниатюры)
    const allImgs = document.querySelectorAll('img');
    const photoUrls = Array.from(allImgs)
      .filter(el => el.src && el.src.includes('avito.st') && el.width > 200)
      .slice(0, 3)
      .map(el => el.src)
      .filter(Boolean);

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
        body: JSON.stringify({title, price, region, description, photos})
      });
      const d = await resp.json();
      showWidget(d, price);
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
  function showWidget(d, price) {
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
      '<div style="background:#1a1a2e;color:#fff;padding:12px 16px;display:flex;justify-content:space-between;align-items:center"><b>IndMart</b><span style="cursor:pointer" onclick="this.closest(\'#indmart-widget\').remove()">✕</span></div>' +
      '<div style="background:'+v[0]+';color:#fff;padding:10px;text-align:center;font-weight:700">'+v[1]+'</div>' +
      banners +
      '<div style="padding:14px 16px">' +
      '<div style="font-weight:600;margin-bottom:8px">'+name+'</div>' +
      '<div style="color:#444;margin-bottom:4px">Рынок: <b>'+d.market_min.toLocaleString('ru')+' – '+d.market_max.toLocaleString('ru')+' ₽</b></div>' +
      '<div style="background:#f5f5f5;border-radius:8px;padding:10px;margin:10px 0;line-height:1.8">💰 Маржа: <b>'+d.reseller_margin.toLocaleString('ru')+' ₽</b><br>⏱ Оборот: <b>'+d.turnover_days+'</b><br>📊 Спрос: <b>'+demand+'</b>'+condLine+'</div>' +
      '<div style="font-style:italic;color:#666;font-size:13px;margin-bottom:12px">"'+d.comment+'"</div>' +
      '<a href="https://indmart.ru" target="_blank" style="display:block;background:#1a1a2e;color:#fff;text-align:center;padding:10px;border-radius:8px;text-decoration:none;font-weight:600">Открыть в IndMart</a>' +
      '</div>';
    document.body.appendChild(w);
  }
  let lastUrl = '';
  setInterval(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      document.getElementById('indmart-btn')?.remove();
      document.getElementById('indmart-widget')?.remove();
    }
    makeButton();
  }, 1000);
  console.log('IndMart loaded');
})();
