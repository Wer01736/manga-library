const pageTitleElement = document.querySelector("#page-title");
const pageHostElement = document.querySelector("#page-host");
const summaryElement = document.querySelector("#queue-summary");
const statusElement = document.querySelector("#status");
const rootSelectElement = document.querySelector("#root-select");
const saveButtonElement = document.querySelector("#save-button");
const fileInputElement = document.querySelector("#url-file");
const importButtonElement = document.querySelector("#import-button");
const importSummaryElement = document.querySelector("#import-summary");
let currentPageUrl = null;

function showStatus(message, type = "") {
  statusElement.textContent = message;
  statusElement.className = ["status", type].filter(Boolean).join(" ");
}

function supportedUrl(value) {
  try {
    const url = new URL(value);
    const validProtocol = ["http:", "https:"].includes(url.protocol);
    const wnacg = ["wnacg.com", "wnacg.ru"].some(host => url.hostname === host || url.hostname.endsWith(`.${host}`));
    const comic18 = ["18comic.vip", "www.18comic.vip"].includes(url.hostname) && /^\/(album|photo)\/\d+/.test(url.pathname);
    const nhentai = ["nhentai.net", "www.nhentai.net"].includes(url.hostname) && /^\/g\/\d+(?:\/\d+)?\/?$/.test(url.pathname);
    return validProtocol && (wnacg || comic18 || nhentai) ? url : null;
  } catch {
    return null;
  }
}

function extractSupportedUrls(text) {
  const matches = text.match(/https?:\/\/[^\s<>"']+/gi) || [];
  const urls = matches
    .map(value => value.replace(/[\])}>.,;，。；]+$/g, ""))
    .map(value => supportedUrl(value)?.href)
    .filter(Boolean);
  return [...new Set(urls)];
}

function refreshImportButton() {
  importButtonElement.disabled = !rootSelectElement.value || !fileInputElement.files?.length;
}

async function sendCurrentPage(rootPath) {
  saveButtonElement.disabled = true;
  summaryElement.textContent = "正在傳送網址…";
  showStatus("正在檢查並加入本機下載佇列…");
  const response = await chrome.runtime.sendMessage({
    type: "SEND_TO_EXISTING_DOWNLOADER",
    sourceUrl: currentPageUrl.href,
    rootPath
  });
  if (!response?.ok) {
    summaryElement.textContent = "傳送失敗";
    showStatus(response?.error || "無法連接原本的下載視窗。", "error");
    saveButtonElement.disabled = false;
    return;
  }

  summaryElement.textContent = `已加入 ${response.queued || 1} 部`;
  showStatus(response.rejected?.length
    ? `已加入下載；另有 ${response.rejected.length} 部未通過檢查。`
    : "請在漫畫書庫查看重複與下載狀態。", "success");
}

async function loadRoots() {
  const response = await fetch("http://127.0.0.1:8766/api/roots");
  if (!response.ok) throw new Error("漫畫書庫服務無法讀取下載位置");
  const roots = await response.json();
  rootSelectElement.innerHTML = '<option value="">請選擇下載位置</option>';
  for (const root of roots) {
    const option = document.createElement("option");
    option.value = root.path;
    option.textContent = `${root.path}（目前 ${root.comic_count || 0} 本）`;
    rootSelectElement.append(option);
  }
  rootSelectElement.disabled = false;
  return roots;
}

async function initialize() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentPageUrl = supportedUrl(tab?.url || "");
  pageTitleElement.textContent = tab?.title || "未命名頁面";
  pageHostElement.textContent = currentPageUrl?.hostname || "此頁只用來變更下載位置";

  const roots = await loadRoots();
  const { selectedRootPath = "" } = await chrome.storage.local.get("selectedRootPath");
  const savedRoot = roots.find(root => root.path === selectedRootPath);
  if (savedRoot) rootSelectElement.value = savedRoot.path;

  saveButtonElement.disabled = !rootSelectElement.value;
  saveButtonElement.textContent = currentPageUrl ? "設定並下載" : "儲存下載位置";

  rootSelectElement.addEventListener("change", () => {
    saveButtonElement.disabled = !rootSelectElement.value;
    refreshImportButton();
  });
  saveButtonElement.addEventListener("click", async () => {
    const rootPath = rootSelectElement.value;
    if (!rootPath) return;
    await chrome.storage.local.set({ selectedRootPath: rootPath });
    if (currentPageUrl) await sendCurrentPage(rootPath);
    else {
      summaryElement.textContent = "位置已儲存";
      showStatus("之後在支援的漫畫頁點擴充功能即可直接下載。", "success");
    }
  });

  fileInputElement.addEventListener("change", () => {
    importSummaryElement.textContent = fileInputElement.files?.[0]?.name || "尚未選擇文件";
    refreshImportButton();
  });
  importButtonElement.addEventListener("click", async () => {
    const rootPath = rootSelectElement.value;
    const file = fileInputElement.files?.[0];
    if (!rootPath || !file) return;
    if (file.size > 5 * 1024 * 1024) {
      showStatus("文件超過 5 MB，請改用較小的文字文件。", "error");
      return;
    }
    importButtonElement.disabled = true;
    const urls = extractSupportedUrls(await file.text());
    if (!urls.length) {
      importSummaryElement.textContent = "沒有找到支援的 WNACG、18comic 或 nhentai 網址";
      showStatus("請確認文件中包含完整的 http 或 https 網址。", "error");
      refreshImportButton();
      return;
    }
    await chrome.storage.local.set({ selectedRootPath: rootPath });
    importSummaryElement.textContent = `找到 ${urls.length} 個網址，正在送出…`;
    showStatus("正在逐筆檢查並加入本機下載佇列…");
    const response = await chrome.runtime.sendMessage({
      type: "SEND_BATCH_TO_EXISTING_DOWNLOADER",
      sourceUrls: urls,
      rootPath
    });
    if (!response?.ok) {
      importSummaryElement.textContent = `共 ${urls.length} 個網址，傳送失敗`;
      showStatus(response?.error || "無法連接原本的下載視窗。", "error");
      refreshImportButton();
      return;
    }
    const queued = response.queued || urls.length;
    const rejected = response.rejected?.length || 0;
    summaryElement.textContent = `已加入 ${queued} 部`;
    importSummaryElement.textContent = `已從 ${file.name} 加入 ${queued} 個網址${rejected ? `；${rejected} 個未通過檢查` : ''}`;
    showStatus("可到漫畫書庫查看逐筆檢查與下載狀態。", "success");
  });

  if (currentPageUrl && savedRoot) {
    saveButtonElement.classList.add("hidden");
    await sendCurrentPage(savedRoot.path);
  } else if (currentPageUrl) {
    summaryElement.textContent = "首次使用，請選擇位置";
    showStatus("選擇一次下載位置後，這一頁會立即送出。");
  } else {
    summaryElement.textContent = "未傳送";
    showStatus("可在這裡變更並儲存下載位置。");
  }
  refreshImportButton();
}

initialize().catch(error => {
  summaryElement.textContent = "傳送失敗";
  showStatus("發生錯誤：" + error.message, "error");
});
