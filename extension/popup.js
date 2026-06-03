const toggle = document.getElementById('toggle');
const stat = document.getElementById('stat');

chrome.storage.sync.get('enabled', data => {
  toggle.checked = data.enabled !== false;
});

toggle.addEventListener('change', () => {
  chrome.storage.sync.set({enabled: toggle.checked});
});

const today = new Date().toDateString();
chrome.storage.local.get(['limitDate', 'limitCount'], data => {
  const count = data.limitDate === today ? (data.limitCount || 0) : 0;
  stat.textContent = `Проверено сегодня: ${count} / 10`;
});
