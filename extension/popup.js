const toggle = document.getElementById('toggle');
const stat = document.getElementById('stat');
const minMargin = document.getElementById('minMargin');
const maxTurnover = document.getElementById('maxTurnover');
const privateOnly = document.getElementById('privateOnly');
const urgentOnly = document.getElementById('urgentOnly');
const saveBtn = document.getElementById('saveFilters');
const savedMsg = document.getElementById('saved');

chrome.storage.sync.get(['enabled', 'filters'], data => {
  toggle.checked = data.enabled !== false;
  if (data.filters) {
    if (data.filters.minMargin) minMargin.value = data.filters.minMargin;
    if (data.filters.maxTurnover) maxTurnover.value = data.filters.maxTurnover;
    privateOnly.checked = data.filters.privateOnly || false;
    urgentOnly.checked = data.filters.urgentOnly || false;
  }
});

toggle.addEventListener('change', () => {
  chrome.storage.sync.set({enabled: toggle.checked});
});

saveBtn.addEventListener('click', () => {
  const margin = parseInt(minMargin.value) || 0;
  const turnover = parseInt(maxTurnover.value) || 0;
  const filters = {
    minMargin: margin >= 0 ? margin : 0,
    maxTurnover: turnover > 0 ? turnover : 0,
    privateOnly: privateOnly.checked,
    urgentOnly: urgentOnly.checked
  };
  chrome.storage.sync.set({filters});
  savedMsg.style.display = 'block';
  setTimeout(() => savedMsg.style.display = 'none', 2000);
});

const today = new Date().toDateString();
chrome.storage.local.get(['limitDate', 'limitCount'], data => {
  const count = data.limitDate === today ? (data.limitCount || 0) : 0;
  stat.textContent = `Проверено сегодня: ${count} / 10`;
});

const modeReseller = document.getElementById('modeReseller');
const modeBuyer = document.getElementById('modeBuyer');
const modeDesc = document.getElementById('modeDesc');
const filtersSection = document.querySelector('.section');

chrome.storage.sync.get('userMode', ({userMode}) => {
  setMode(userMode || 'reseller');
});

function setMode(mode) {
  if (mode === 'reseller') {
    modeReseller.classList.add('active');
    modeBuyer.classList.remove('active');
    if (filtersSection) filtersSection.style.display = 'block';
    modeDesc.textContent = 'Оценка маржи и срока оборота для перепродажи';
  } else {
    modeBuyer.classList.add('active');
    modeReseller.classList.remove('active');
    if (filtersSection) filtersSection.style.display = 'none';
    modeDesc.textContent = '🚧 В разработке — скоро расчёт окупаемости для покупателей';
  }
  chrome.storage.sync.set({userMode: mode});
}

modeReseller.addEventListener('click', () => setMode('reseller'));
modeBuyer.addEventListener('click', () => setMode('buyer'));
