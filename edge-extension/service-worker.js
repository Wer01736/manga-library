const BRIDGE_URL = "http://127.0.0.1:8766";
const EXTENSION_VERSION = "0.7.2";
const NHENTAI_HOSTS = new Set(["nhentai.net", "www.nhentai.net"]);
let resolverQueue = Promise.resolve();
const resolvedNhentaiSources = new Map();
const DELIVERY_LIMIT = 3;
let activeDeliveries = 0;
const deliveryWaiters = [];

async function withDeliverySlot(task) {
  if (activeDeliveries >= DELIVERY_LIMIT) {
    await new Promise(resolve => deliveryWaiters.push(resolve));
  }
  activeDeliveries += 1;
  try {
    return await task();
  } finally {
    activeDeliveries -= 1;
    deliveryWaiters.shift()?.();
  }
}

function albumIdFromUrl(value) {
  try {
    return new URL(value).pathname.match(/^\/(?:album|photo)\/(\d+)/)?.[1] || "";
  } catch {
    return "";
  }
}

function nhentaiIdFromUrl(value) {
  try {
    const url = new URL(value);
    if (!NHENTAI_HOSTS.has(url.hostname)) return "";
    return url.pathname.match(/^\/g\/(\d+)(?:\/\d+)?\/?$/)?.[1] || "";
  } catch {
    return "";
  }
}

async function reportDiagnostic(stage, albumId, detail = "") {
  try {
    await fetch("http://127.0.0.1:8766/api/18comic/diagnostic", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage, album_id: albumId, detail })
    });
  } catch { /* 診斷不可影響下載 */ }
}

function waitForTab(tabId, timeoutMs = 15000, timeoutLabel = "頁面") {
  return new Promise((resolve, reject) => {
    let done = false;
    let pollTimer;
    const finish = error => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      clearInterval(pollTimer);
      chrome.tabs.onUpdated.removeListener(listener);
      error ? reject(error) : resolve();
    };
    const listener = (id, change) => {
      if (id === tabId && change.status === "complete") finish();
    };
    const documentIsReady = async () => {
      try {
        const [execution] = await chrome.scripting.executeScript({
          target: { tabId },
          func: () => ["interactive", "complete"].includes(document.readyState)
        });
        if (execution?.result) finish();
      } catch { /* 分頁導向期間尚不能執行，繼續等待 */ }
    };
    const timer = setTimeout(() => finish(new Error(`${timeoutLabel}開啟逾時`)), timeoutMs);
    pollTimer = setInterval(documentIsReady, 500);
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId, tab => {
      if (chrome.runtime.lastError) return finish(new Error(chrome.runtime.lastError.message));
      if (tab.status === "complete") finish();
    });
    documentIsReady();
  });
}

async function bridgeRequest(path, payload) {
  const response = await fetch(`${BRIDGE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.detail || "本機漫畫書庫沒有接受這筆網址");
  return result;
}

async function sendToExistingDownloader(sourceUrls, rootPath) {
  let queued = 0;
  const rejected = [];
  for (const sourceUrl of sourceUrls) {
    try {
      let resolvedNhentai = null;
      if (albumIdFromUrl(sourceUrl)) await resolve18comic(sourceUrl);
      else if (nhentaiIdFromUrl(sourceUrl)) resolvedNhentai = await resolveNhentai(sourceUrl);
      const preview = await bridgeRequest("/api/web-download/batch-preview", {
        urls: [sourceUrl], root_path: rootPath
      });
      const item = preview.items?.[0];
      if (!item) throw new Error("本機漫畫書庫沒有回傳檢查結果");
      if (item.status !== "ready") throw new Error(item.reason || "這筆網址未通過下載檢查");
      const job = await bridgeRequest("/api/web-download/start", {
        url: sourceUrl,
        root_path: rootPath,
        tags: item.source_tags || [],
        delivery_mode: resolvedNhentai ? "browser" : "server"
      });
      if (resolvedNhentai) {
        await deliverNhentaiPages(job.id, resolvedNhentai.images, resolvedNhentai.readerUrls, nhentaiIdFromUrl(sourceUrl));
      }
      queued += 1;
    } catch (error) {
      const nhentaiId = nhentaiIdFromUrl(sourceUrl);
      if (nhentaiId) await reportDiagnostic("nhentai_resolve_failed", nhentaiId, error.message);
      rejected.push(`${sourceUrl}：${error.message}`);
    }
  }
  if (!queued) throw new Error(rejected.join("；") || "沒有網址可加入下載");
  return { queued, rejected };
}

async function fetchNhentaiCandidate(candidate, readerUrl, attempt) {
  const controller = new AbortController();
  let timer;
  const request = (async () => {
    const response = await fetch(candidate, {
      method: "GET",
      credentials: "include",
      cache: attempt === 1 ? "force-cache" : "reload",
      referrer: readerUrl,
      signal: controller.signal
    });
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.httpStatus = response.status;
      throw error;
    }
    if (contentType && !contentType.toLowerCase().startsWith("image/")) {
      throw new Error(`非圖片內容 ${contentType}`);
    }
    return { candidate, contentType: contentType || "application/octet-stream", data: await response.arrayBuffer() };
  })();
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      const error = new Error("連線逾時");
      error.isTimeout = true;
      reject(error);
    }, 20000);
  });
  try {
    return await Promise.race([request, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

async function fetchNhentaiPage(entry, readerUrl) {
  const candidates = Array.isArray(entry) ? entry : [entry];
  const errors = [];
  for (const candidate of candidates) {
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        return await fetchNhentaiCandidate(candidate, readerUrl, attempt);
      } catch (error) {
        errors.push(`${error.message}（第 ${attempt} 次）：${candidate}`);
        if (error.httpStatus && error.httpStatus < 500) break;
        if (attempt < 3) await new Promise(resolve => setTimeout(resolve, 1000 * attempt));
      }
    }
  }
  throw new Error(errors.join("；") || "所有圖片候選網址皆失敗");
}

async function deliverNhentaiPages(jobId, images, readerUrls, galleryId, startIndex = 0) {
  await reportDiagnostic("nhentai_browser_delivery_started", galleryId, String(images.length));
  try {
    for (let index = startIndex; index < images.length; index += 1) {
      let page;
      try {
        page = await fetchNhentaiPage(images[index], readerUrls[index]);
      } catch (error) {
        throw new Error(`第 ${index + 1} / ${images.length} 頁：${error.message}`);
      }
      const response = await fetch(`${BRIDGE_URL}/api/web-download/${encodeURIComponent(jobId)}/browser-page?index=${index + 1}&url=${encodeURIComponent(page.candidate)}`, {
        method: "POST",
        headers: { "Content-Type": page.contentType },
        body: page.data
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || `本機拒絕第 ${index + 1} 頁`);
    }
    await reportDiagnostic("nhentai_browser_delivery_completed", galleryId, String(images.length));
  } catch (error) {
    await bridgeRequest(`/api/web-download/${encodeURIComponent(jobId)}/browser-error`, { error: error.message }).catch(() => undefined);
    await reportDiagnostic("nhentai_browser_delivery_failed", galleryId, error.message);
    throw error;
  }
}

async function resumeBrowserJobs() {
  try {
    const response = await fetch(`${BRIDGE_URL}/api/web-download`);
    if (!response.ok) return;
    const jobs = await response.json();
    const resumable = jobs.filter(job =>
      job.delivery_mode === "browser" &&
      ["waiting", "downloading"].includes(job.status) &&
      nhentaiIdFromUrl(job.source_url)
    );
    for (const job of resumable) {
      withDeliverySlot(async () => {
        const resolved = await resolveNhentai(job.source_url);
        await deliverNhentaiPages(
          job.id,
          resolved.images,
          resolved.readerUrls,
          nhentaiIdFromUrl(job.source_url),
          Number(job.completed || 0)
        );
      }).catch(error => reportDiagnostic("nhentai_resume_failed", nhentaiIdFromUrl(job.source_url), error.message));
    }
  } catch { /* 後端尚未啟動時保留原佇列 */ }
}

chrome.runtime.onInstalled.addListener(() => resumeBrowserJobs());
chrome.runtime.onStartup.addListener(() => resumeBrowserJobs());

async function scrape18comicAlbum(tabId, albumId) {
  const [execution] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [albumId],
    func: expectedId => {
      const title = document.querySelector("#book-name")?.textContent?.trim() || "";
      const section = label => [...document.querySelectorAll("h2")]
        .find(node => node.textContent.replace(/\s+/g, "").startsWith(label))?.parentElement;
      const texts = node => [...(node?.querySelectorAll("a") || [])]
        .map(link => link.textContent.trim()).filter(Boolean);
      const author = texts(section("作者："))[0] || "";
      const tags = texts(section("分類標籤："));
      const descriptionNode = [...document.querySelectorAll("h2")]
        .find(node => node.textContent.replace(/\s+/g, "").startsWith("敘述："));
      const description = descriptionNode?.textContent.replace(/^\s*敘述：\s*/, "").trim() || "";
      const cover = document.querySelector("#album_photo_cover img");
      const coverUrl = cover?.dataset.original || cover?.src || "";
      const pageMatch = document.body.innerText.match(/頁數[：:]\s*(\d+)/);
      if (!title || !document.querySelector(`a[href*="/photo/${expectedId}"]`)) {
        throw new Error("18comic 目錄頁尚未載入；若看到驗證頁，請先在瀏覽器完成驗證或登入");
      }
      return { title, author, tags, description, cover_url: coverUrl, page_count: Number(pageMatch?.[1] || 0) };
    }
  });
  return execution?.result;
}

async function scrape18comicReader(tabId, albumId, expectedPages = 0) {
  // Keep the reader tab in the background. Each injected call is synchronous;
  // the extension worker owns the polling loop, so Edge cannot detach a long
  // page-side async task and leave the temporary tab open forever.
  const deadline = Date.now() + 90000;
  const collectedByIndex = new Map();
  const collectedWithoutIndex = [];
  const knownUrls = new Set();
  let scrambleId = 220980;
  let unchangedAtBottom = 0;
  let lastReported = 0;

  const mergeCandidate = (candidate, index = 0) => {
    if (!candidate || typeof candidate !== "string") return;
    if (knownUrls.has(candidate)) {
      if (index > 0 && !collectedByIndex.has(index)) {
        collectedByIndex.set(index, candidate);
        const oldIndex = collectedWithoutIndex.indexOf(candidate);
        if (oldIndex >= 0) collectedWithoutIndex.splice(oldIndex, 1);
      }
      return;
    }
    knownUrls.add(candidate);
    if (index > 0 && !collectedByIndex.has(index)) collectedByIndex.set(index, candidate);
    else collectedWithoutIndex.push(candidate);
  };

  while (Date.now() < deadline) {
    const previousCount = knownUrls.size;
    const [execution] = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      args: [albumId],
      func: expectedId => {
        const items = [];
        const seen = new Set();
        const addCandidate = (candidate, index = 0) => {
          if (typeof candidate !== "string" || !candidate.trim()) return;
          const first = candidate.trim().split(/\s+/)[0];
          let absolute;
          try {
            absolute = new URL(first, location.href).href;
          } catch {
            return;
          }
          if (!absolute.includes("/media/photos/" + expectedId + "/") || seen.has(absolute)) return;
          seen.add(absolute);
          items.push({ index, url: absolute });
        };
        const selector = [
          "img[id^=\"album_photo_\"]",
          "img[data-original*=\"/media/photos/" + expectedId + "/\"]",
          "img[data-src*=\"/media/photos/" + expectedId + "/\"]",
          "img[src*=\"/media/photos/" + expectedId + "/\"]"
        ].join(",");
        for (const image of document.querySelectorAll(selector)) {
          const index = Number(image.id?.match(/^album_photo_(\d+)/)?.[1] || 0);
          for (const candidate of [
            image.getAttribute("data-original"),
            image.getAttribute("data-src"),
            image.getAttribute("data-lazy-src"),
            image.currentSrc,
            image.src,
            image.srcset
          ]) addCandidate(candidate, index);
        }
        const scriptText = [...document.scripts].map(script => script.textContent || "").join("\n");
        const currentTop = Math.max(window.scrollY, document.documentElement.scrollTop, document.body?.scrollTop || 0);
        const maximumTop = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
        const nextTop = Math.min(maximumTop, currentTop + Math.max(600, Math.floor(window.innerHeight * 0.8)));
        window.scrollTo(0, nextTop);
        window.dispatchEvent(new Event("scroll"));
        return {
          items,
          scramble_id: Number(scriptText.match(/(?:var\s+)?scramble_id\s*=\s*[\"']?(\d+)/)?.[1] || 220980),
          at_bottom: currentTop >= maximumTop - 4
        };
      }
    });
    const batch = execution?.result || {};
    scrambleId = Number(batch.scramble_id || scrambleId);
    for (const item of batch.items || []) mergeCandidate(item.url, Number(item.index || 0));
    const found = knownUrls.size;
    if (found > lastReported && (found >= expectedPages || found - lastReported >= 10)) {
      lastReported = found;
      await reportDiagnostic("reader_progress", albumId, `${found}/${expectedPages || "?"}`);
    }
    if (expectedPages > 0 && found >= expectedPages) break;
    if (batch.at_bottom && found === previousCount) unchangedAtBottom += 1;
    else unchangedAtBottom = 0;
    if (batch.at_bottom && unchangedAtBottom >= 40) break;
    await new Promise(resolve => setTimeout(resolve, 500));
  }

  const images = [
    ...[...collectedByIndex.entries()].sort((left, right) => left[0] - right[0]).map(item => item[1]),
    ...collectedWithoutIndex
  ];
  if (!images.length) {
    throw new Error("閱讀頁沒有找到漫畫圖片；請確認此作品能在瀏覽器正常閱讀");
  }
  return { images, scramble_id: scrambleId };
}

async function resolve18comic(sourceUrl) {
  const parsed = new URL(sourceUrl);
  const match = parsed.pathname.match(/^\/(?:album|photo)\/(\d+)/);
  if (!["18comic.vip", "www.18comic.vip"].includes(parsed.hostname) || !match) {
    throw new Error("不是支援的 18comic 目錄或閱讀網址");
  }
  const albumId = match[1];
  await reportDiagnostic("resolve_started", albumId);
  const albumUrl = `https://18comic.vip/album/${albumId}`;
  const photoUrl = `https://18comic.vip/photo/${albumId}`;
  const tab = await chrome.tabs.create({ url: albumUrl, active: false });
  try {
    await waitForTab(tab.id, 45000, "18comic 目錄頁");
    await reportDiagnostic("album_loaded", albumId);
    const album = await scrape18comicAlbum(tab.id, albumId);
    await reportDiagnostic("album_scraped", albumId, album.title);
    await chrome.tabs.update(tab.id, { url: photoUrl, active: false });
    await waitForTab(tab.id, 45000, "18comic 閱讀頁");
    await reportDiagnostic("reader_loaded", albumId);
    const reader = await scrape18comicReader(tab.id, albumId, album.page_count);
    const images = reader.images || [];
    await reportDiagnostic("images_scraped", albumId, String(images.length));
    if (album.page_count && images.length !== album.page_count) {
      throw new Error(`閱讀頁只讀到 ${images.length} / ${album.page_count} 頁，請重新載入來源頁後再試`);
    }
    const response = await fetch("http://127.0.0.1:8766/api/18comic/manifest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        album_id: albumId,
        album_url: albumUrl,
        photo_url: photoUrl,
        title: album.title,
        author: album.author,
        tags: album.tags,
        description: album.description,
        cover_url: album.cover_url,
        extension_version: EXTENSION_VERSION,
        scramble_id: reader.scramble_id || 220980,
        images
      })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "本機下載橋接拒絕漫畫資料");
    await reportDiagnostic("manifest_saved", albumId, String(images.length));
    return result;
  } finally {
    await chrome.tabs.remove(tab.id).catch(() => undefined);
  }
}

function normalizeNhentaiImageUrl(value, baseUrl) {
  if (!value || typeof value !== "string") return "";
  try {
    const normalized = value
      .replace(/\\u002f/gi, "/")
      .replace(/\\\//g, "/")
      .replace(/&amp;/gi, "&")
      .replace(/^\/\//, "https://");
    const url = new URL(normalized, baseUrl);
    url.pathname = url.pathname.replace(/(\.(?:avif|webp|jpe?g|png|gif|bmp))\1$/i, "$1");
    const imageHost = url.hostname === "nhentai.net" || url.hostname.endsWith(".nhentai.net");
    const filename = decodeURIComponent(url.pathname.split("/").pop() || "");
    const isPageImage = url.pathname.includes("/galleries/") && !/^cover\./i.test(filename) && !/^\d+t(?:\.|$)/i.test(filename);
    return imageHost && isPageImage ? url.href : "";
  } catch {
    return "";
  }
}

function imageUrlFromNhentaiHtml(html, readerUrl, expectedGalleryPath = "") {
  const pageNumber = new URL(readerUrl).pathname.match(/\/(\d+)\/?$/)?.[1] || "";
  const normalized = String(html || "")
    .replace(/\\u002f/gi, "/")
    .replace(/\\\//g, "/")
    .replace(/&amp;/gi, "&");
  const rawMatches = normalized.match(/(?:https?:)?\/\/(?:[a-z0-9-]+\.)?nhentai\.net\/galleries\/[^"'\s<>]+/gi) || [];
  const candidates = [...new Set(rawMatches.map(value => normalizeNhentaiImageUrl(value, readerUrl)).filter(Boolean))];
  const exact = candidates.find(value => {
    const url = new URL(value);
    const filename = decodeURIComponent(url.pathname.split("/").pop() || "");
    return (!expectedGalleryPath || url.pathname.startsWith(expectedGalleryPath)) && filename.startsWith(`${pageNumber}.`);
  });
  return exact || candidates.find(value => !expectedGalleryPath || new URL(value).pathname.startsWith(expectedGalleryPath)) || "";
}

function deriveNhentaiImageUrl(thumbnailUrl, firstImageUrl, pageNumber) {
  try {
    const thumbnail = new URL(thumbnailUrl);
    const firstImage = new URL(firstImageUrl);
    const filename = decodeURIComponent(thumbnail.pathname.split("/").pop() || "");
    const match = filename.match(new RegExp(`^${pageNumber}t(\\..+)$`, "i"));
    if (!match || !thumbnail.pathname.includes("/galleries/")) return "";
    const directory = thumbnail.pathname.slice(0, thumbnail.pathname.lastIndexOf("/") + 1);
    const firstDirectory = firstImage.pathname.slice(0, firstImage.pathname.lastIndexOf("/") + 1);
    if (directory !== firstDirectory) return "";
    const result = new URL(firstImage.href);
    result.pathname = `${directory}${pageNumber}${match[1]}`;
    result.search = "";
    result.hash = "";
    return normalizeNhentaiImageUrl(result.href, firstImageUrl);
  } catch {
    return "";
  }
}

function nhentaiImageCandidates(thumbnailUrl, firstImageUrl, pageNumber) {
  try {
    const first = new URL(firstImageUrl);
    const directory = first.pathname.slice(0, first.pathname.lastIndexOf("/") + 1);
    const firstFilename = decodeURIComponent(first.pathname.split("/").pop() || "");
    const firstSuffix = firstFilename.match(/^\d+(\..+)$/)?.[1] || ".webp";
    const values = new Set();
    const add = value => {
      const normalized = normalizeNhentaiImageUrl(value, firstImageUrl);
      if (normalized) values.add(normalized);
    };
    const derived = deriveNhentaiImageUrl(thumbnailUrl, firstImageUrl, pageNumber);
    if (derived) {
      add(derived);
    }
    for (const suffix of [firstSuffix, ".webp", ".jpg", ".png", ".jpeg"]) {
      const candidate = new URL(first.href);
      candidate.pathname = `${directory}${pageNumber}${suffix}`;
      candidate.search = "";
      candidate.hash = "";
      add(candidate.href);
    }
    return [...values];
  } catch {
    return [];
  }
}

async function scrapeNhentaiGallery(tabId, galleryId) {
  const [execution] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [galleryId],
    func: async expectedId => {
      const deadline = Date.now() + 30000;
      const textOf = node => node?.textContent?.replace(/\s+/g, " ").trim() || "";
      const linkName = link => textOf(link.querySelector(".name")) || decodeURIComponent(new URL(link.href).pathname.split("/").filter(Boolean).pop() || "").replace(/-/g, " ");
      while (Date.now() < deadline) {
        const info = document.querySelector("#info") || document;
        const title = textOf(info.querySelector("h1.title"));
        const pageMatch = document.body.innerText.match(/Pages:\s*(\d+)/i);
        const pageCount = Number(pageMatch?.[1] || 0);
        if (title && pageCount) {
          const links = [...info.querySelectorAll("a[href]")];
          const tags = links.filter(link => new URL(link.href).pathname.startsWith("/tag/")).map(linkName);
          const authors = links.filter(link => new URL(link.href).pathname.startsWith("/artist/")).map(linkName);
          const languages = links.filter(link => new URL(link.href).pathname.startsWith("/language/")).map(linkName).map(value => value.toLowerCase());
          const supportedLanguages = languages.filter(value => ["chinese", "japanese", "english", "korean"].includes(value));
          const cover = document.querySelector("#cover img");
          const galleryThumbLinks = [...document.querySelectorAll(`a.gallerythumb[href*="/g/${expectedId}/"]`)];
          const readerLinks = galleryThumbLinks.length
            ? galleryThumbLinks
            : [...document.querySelectorAll(`a[href*="/g/${expectedId}/"]`)];
          const readerItems = readerLinks
            .map(link => {
              const url = new URL(link.href, location.href);
              const page = Number(url.pathname.match(new RegExp(`^/g/${expectedId}/(\\d+)/?$`))?.[1] || 0);
              const image = link.querySelector("img");
              const thumbnail = image?.dataset.src || image?.dataset.original || image?.currentSrc || image?.src || "";
              return page ? { page, url: url.href, thumbnail: thumbnail ? new URL(thumbnail, location.href).href : "" } : null;
            })
            .filter(Boolean)
            .sort((a, b) => a.page - b.page)
            .filter((item, index, all) => index === 0 || item.page !== all[index - 1].page);
          const readerUrls = readerItems.map(item => item.url);
          return {
            title,
            original_title: textOf(info.querySelector("h2.title")),
            author: [...new Set(authors)].join(", "),
            tags: [...new Set(tags)],
            languages: [...new Set(supportedLanguages)],
            language: supportedLanguages[0] || "unknown",
            cover_url: cover?.dataset.src || cover?.dataset.original || cover?.currentSrc || cover?.src || "",
            page_count: pageCount,
            reader_urls: readerUrls.length === pageCount
              ? readerUrls
              : Array.from({ length: pageCount }, (_, index) => `https://nhentai.net/g/${expectedId}/${index + 1}/`),
            thumbnail_urls: readerItems.length === pageCount ? readerItems.map(item => item.thumbnail) : []
          };
        }
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      throw new Error("nhentai 目錄頁尚未載入；請先確認作品頁可在瀏覽器正常開啟");
    }
  });
  return execution?.result;
}

async function fetchNhentaiReaderImage(readerUrl, expectedGalleryPath = "") {
  try {
    const response = await fetch(readerUrl, { credentials: "include", cache: "no-store" });
    if (!response.ok) return "";
    return imageUrlFromNhentaiHtml(await response.text(), readerUrl, expectedGalleryPath);
  } catch {
    return "";
  }
}

async function scrapeNhentaiReader(tabId, readerUrl, expectedGalleryPath = "") {
  const [execution] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [readerUrl, expectedGalleryPath],
    func: async (expectedUrl, galleryPath) => {
      const deadline = Date.now() + 30000;
      const pageNumber = new URL(expectedUrl).pathname.match(/\/(\d+)\/?$/)?.[1] || "";
      let lastCandidates = [];
      while (Date.now() < deadline) {
        const areas = new Map([...document.images].map(image => [
          image.dataset.src || image.dataset.original || image.currentSrc || image.src || "",
          Number(image.naturalWidth || 0) * Number(image.naturalHeight || 0)
        ]));
        const attributeValues = [...document.querySelectorAll("[src], [data-src], [data-original], [href]")]
          .flatMap(node => [node.getAttribute("src"), node.getAttribute("data-src"), node.getAttribute("data-original"), node.getAttribute("href")]);
        const resourceValues = performance.getEntriesByType("resource").map(entry => entry.name);
        const candidates = [...new Set([...attributeValues, ...resourceValues].filter(Boolean))]
          .map(value => {
            try {
              const url = new URL(value, location.href);
              const filename = decodeURIComponent(url.pathname.split("/").pop() || "");
              const validHost = url.hostname === "nhentai.net" || url.hostname.endsWith(".nhentai.net");
              const validPath = url.pathname.includes("/galleries/") && (!galleryPath || url.pathname.startsWith(galleryPath));
              const notThumbnail = !/^cover\./i.test(filename) && !/^\d+t(?:\.|$)/i.test(filename);
              const validPage = filename.startsWith(`${pageNumber}.`) || filename.startsWith(`${pageNumber}_`);
              return validHost && validPath && notThumbnail
                ? { url: url.href, area: areas.get(value) || 0, validPage }
                : null;
            } catch {
              return null;
            }
          })
          .filter(Boolean)
          .sort((a, b) => Number(b.validPage) - Number(a.validPage) || b.area - a.area);
        lastCandidates = candidates.slice(0, 8).map(item => item.url);
        const exact = candidates.find(item => item.validPage);
        if (exact) return exact.url;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      throw new Error(`閱讀頁 ${pageNumber || "?"} 沒有找到高畫質漫畫圖片；目前網址 ${location.href}；候選 ${lastCandidates.join(" | ") || "無"}`);
    }
  });
  return execution?.result || "";
}

async function mapWithLimit(items, limit, worker) {
  const results = new Array(items.length);
  let cursor = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const index = cursor++;
      results[index] = await worker(items[index], index);
    }
  });
  await Promise.all(runners);
  return results;
}

async function collectNhentaiImages(readerUrls, thumbnailUrls, galleryId) {
  let firstImage = await fetchNhentaiReaderImage(readerUrls[0]);
  await reportDiagnostic(firstImage ? "nhentai_first_image_fetched" : "nhentai_first_fetch_missing", galleryId, firstImage || readerUrls[0]);
  if (!firstImage) {
    const firstTab = await chrome.tabs.create({ url: readerUrls[0], active: false });
    try {
      await waitForTab(firstTab.id, 45000, "nhentai 第一頁");
      firstImage = await scrapeNhentaiReader(firstTab.id, readerUrls[0]);
      await reportDiagnostic("nhentai_first_image_tab", galleryId, firstImage);
    } finally {
      await chrome.tabs.remove(firstTab.id).catch(() => undefined);
    }
  }
  if (!firstImage) throw new Error("nhentai 第一頁沒有找到高畫質圖片");
  if (thumbnailUrls.length === readerUrls.length) {
    const derived = thumbnailUrls.map((thumbnailUrl, index) => deriveNhentaiImageUrl(thumbnailUrl, firstImage, index + 1));
    derived[0] = firstImage;
    const firstDerived = derived[0] ? new URL(derived[0]) : null;
    const actualFirst = new URL(firstImage);
    const ruleMatches = firstDerived && firstDerived.hostname === actualFirst.hostname && firstDerived.pathname === actualFirst.pathname;
    if (ruleMatches && derived.every(Boolean) && new Set(derived).size === readerUrls.length) {
      await reportDiagnostic("nhentai_thumbnail_rule_verified", galleryId, `${derived.length} candidates；${firstImage}`);
    } else {
      await reportDiagnostic("nhentai_thumbnail_rule_rejected", galleryId, `${derived.filter(Boolean).length}/${readerUrls.length}；first=${derived[0] || "missing"}`);
    }
  }
  const firstUrl = new URL(firstImage);
  const galleryPath = firstUrl.pathname.slice(0, firstUrl.pathname.lastIndexOf("/") + 1);
  const images = new Array(readerUrls.length);
  images[0] = firstImage;
  const unresolved = [];
  const remaining = await mapWithLimit(readerUrls.slice(1), 6, async (readerUrl, index) => {
    const image = await fetchNhentaiReaderImage(readerUrl, galleryPath);
    if (!image) unresolved.push({ index: index + 1, readerUrl });
    return image;
  });
  remaining.forEach((image, index) => { images[index + 1] = image; });
  await reportDiagnostic("nhentai_fast_reader_result", galleryId, `${readerUrls.length - unresolved.length}/${readerUrls.length}；待補 ${unresolved.length}`);
  if (unresolved.length) {
    if (thumbnailUrls.length !== readerUrls.length) {
      throw new Error(`缺少縮圖路徑，無法建立 ${unresolved.length} 頁的原圖候選清單`);
    }
    for (const item of unresolved) {
      images[item.index] = nhentaiImageCandidates(thumbnailUrls[item.index], firstImage, item.index + 1);
    }
    await reportDiagnostic("nhentai_candidate_lists_created", galleryId, `${unresolved.length} pages`);
  }
  const missing = images.filter(image => !image || (Array.isArray(image) && !image.length)).length;
  if (missing) {
    throw new Error(`高畫質圖片清單不完整：缺少 ${missing} / ${readerUrls.length} 頁`);
  }
  return images;
}

async function resolveNhentai(sourceUrl) {
  const galleryId = nhentaiIdFromUrl(sourceUrl);
  if (!galleryId) throw new Error("不是支援的 nhentai 目錄或閱讀網址");
  const galleryUrl = `https://nhentai.net/g/${galleryId}/`;
  const tab = await chrome.tabs.create({ url: galleryUrl, active: false });
  let gallery;
  try {
    await reportDiagnostic("nhentai_resolve_started", galleryId);
    await waitForTab(tab.id, 45000, "nhentai 目錄頁");
    gallery = await scrapeNhentaiGallery(tab.id, galleryId);
  } finally {
    await chrome.tabs.remove(tab.id).catch(() => undefined);
  }
  if (!gallery?.reader_urls?.length || gallery.reader_urls.length !== gallery.page_count) {
    throw new Error(`目錄頁只讀到 ${gallery?.reader_urls?.length || 0} / ${gallery?.page_count || 0} 個閱讀網址`);
  }
  await reportDiagnostic("nhentai_gallery_scraped", galleryId, `${gallery.title}；${gallery.language}`);
  const images = await collectNhentaiImages(gallery.reader_urls, gallery.thumbnail_urls || [], galleryId);
  await reportDiagnostic("nhentai_images_scraped", galleryId, String(images.length));
  const response = await fetch(`${BRIDGE_URL}/api/source/manifest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source_site: "nhentai",
      source_id: galleryId,
      gallery_url: galleryUrl,
      reader_url: gallery.reader_urls[0],
      title: gallery.title,
      original_title: gallery.original_title,
      author: gallery.author,
      tags: gallery.tags,
      languages: gallery.languages,
      language: gallery.language,
      description: "",
      cover_url: gallery.cover_url.replace(/(\.(?:avif|webp|jpe?g|png|gif|bmp))\1(?=\?|#|$)/i, "$1"),
      page_count: gallery.page_count,
      extension_version: EXTENSION_VERSION,
      images
    })
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.detail || "本機下載橋接拒絕 nhentai 漫畫資料");
  const resolved = { preview: result, images, readerUrls: gallery.reader_urls };
  resolvedNhentaiSources.set(galleryId, resolved);
  return resolved;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "RESOLVE_SOURCE") {
    const diagnosticId = albumIdFromUrl(message.url) || nhentaiIdFromUrl(message.url);
    reportDiagnostic("request_received", diagnosticId);
    resolverQueue = resolverQueue.catch(() => undefined).then(() => {
      if (albumIdFromUrl(message.url)) return resolve18comic(message.url);
      if (nhentaiIdFromUrl(message.url)) return resolveNhentai(message.url);
      throw new Error("不是支援的 18comic 或 nhentai 網址");
    });
    resolverQueue
      .then(result => sendResponse({
        ok: true,
        preview: result?.preview || result,
        extension_version: EXTENSION_VERSION
      }))
      .catch(async error => {
        await reportDiagnostic("resolve_failed", diagnosticId, error.message);
        sendResponse({ ok: false, error: error.message });
      });
    return true;
  }
  if (message?.type === "DELIVER_NHENTAI") {
    const galleryId = nhentaiIdFromUrl(message.url);
    if (!galleryId || !message.jobId) {
      sendResponse({ ok: false, error: "nhentai 圖片傳送資料不完整" });
      return false;
    }
    withDeliverySlot(async () => {
      const resolved = resolvedNhentaiSources.get(galleryId) || await resolveNhentai(message.url);
      await deliverNhentaiPages(message.jobId, resolved.images, resolved.readerUrls, galleryId);
    })
      .then(() => sendResponse({ ok: true, extension_version: EXTENSION_VERSION }))
      .catch(error => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message?.type === "RESOLVE_18COMIC") {
    const diagnosticAlbumId = albumIdFromUrl(message.url);
    reportDiagnostic("request_received", diagnosticAlbumId);
    resolverQueue = resolverQueue.catch(() => undefined).then(() => resolve18comic(message.url));
    resolverQueue
      .then(preview => sendResponse({ ok: true, preview, extension_version: EXTENSION_VERSION }))
      .catch(async error => {
        await reportDiagnostic("resolve_failed", diagnosticAlbumId, error.message);
        sendResponse({ ok: false, error: error.message });
      });
    return true;
  }
  if (!["SEND_TO_EXISTING_DOWNLOADER", "SEND_BATCH_TO_EXISTING_DOWNLOADER"].includes(message?.type)) return false;
  const sourceUrls = message.type === "SEND_BATCH_TO_EXISTING_DOWNLOADER"
    ? message.sourceUrls
    : [message.sourceUrl];
  withDeliverySlot(() => sendToExistingDownloader(sourceUrls, message.rootPath))
    .then(() => sendResponse({ ok: true }))
    .catch(error => sendResponse({ ok: false, error: error.message }));
  return true;
});
