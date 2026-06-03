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
  function evaluate(title, price) {
    const region = getText(['[data-marker="item-address/name"]','[class*="address"]']);
    const description = getText(['[data-marker="item-view/item-description"]','[itemprop="description"]']).slice(0,300);
    const btn = document.getElementById('indmart-btn');
    if (btn) btn.textContent = '⏳ Анализирую...';
    fetch('https://indmart.ru/api/avito-eval', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({title, price, region, description})
    }).then(r => r.json()).then(d => showWidget(d, price))
      .catch(e => { if (btn) btn.textContent = '❌ Ошибка'; });
  }
  function showWidget(d, price) {
    document.getElementById('indmart-btn')?.remove();
    document.getElementById('indmart-widget')?.remove();
    const verdicts = {flash:['#8b5cf6','⚡ СРОЧНО БЕРИТЕ'],green:['#22c55e','🟢 ХОРОШАЯ ЦЕНА'],yellow:['#f59e0b','🟡 В РЫНКЕ'],red:['#ef4444','🔴 ЗАВЫШЕНО']};
    const v = verdicts[d.verdict] || verdicts.yellow;
    const name = [d.category, d.brand, d.model].filter(Boolean).join(' ');
    const demand = {high:'высокий',medium:'средний',low:'низкий'}[d.demand] || d.demand;
    const w = document.createElement('div');
    w.id = 'indmart-widget';
    w.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:2147483647;width:300px;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,0.18);font-family:system-ui,sans-serif;font-size:14px;background:#fff;overflow:hidden';
    w.innerHTML =
      '<div style="background:#1a1a2e;color:#fff;padding:12px 16px;display:flex;justify-content:space-between;align-items:center"><b>IndMart</b><span style="cursor:pointer" onclick="this.closest(\'#indmart-widget\').remove()">✕</span></div>' +
      '<div style="background:'+v[0]+';color:#fff;padding:10px;text-align:center;font-weight:700">'+v[1]+'</div>' +
      '<div style="padding:14px 16px">' +
      '<div style="font-weight:600;margin-bottom:8px">'+name+'</div>' +
      '<div style="color:#444;margin-bottom:4px">Рынок: <b>'+d.market_min.toLocaleString('ru')+' – '+d.market_max.toLocaleString('ru')+' ₽</b></div>' +
      '<div style="background:#f5f5f5;border-radius:8px;padding:10px;margin:10px 0;line-height:1.8">💰 Маржа: <b>'+d.reseller_margin.toLocaleString('ru')+' ₽</b><br>⏱ Оборот: <b>'+d.turnover_days+'</b><br>📊 Спрос: <b>'+demand+'</b></div>' +
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
