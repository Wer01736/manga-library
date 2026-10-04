window.addEventListener("message", async event => {
  const data = event.data;
  if (event.source !== window || data?.source !== "comic-library") return;
  const runtimeType = data.type === "COMIC_SOURCE_RESOLVE_REQUEST"
    ? "RESOLVE_SOURCE"
    : data.type === "COMIC_NHENTAI_DELIVER_REQUEST"
      ? "DELIVER_NHENTAI"
      : data.type === "COMIC_18_RESOLVE_REQUEST"
        ? "RESOLVE_18COMIC"
        : "";
  if (!runtimeType) return;
  window.postMessage({
    source: "comic-download-extension",
    requestId: data.requestId,
    ack: true
  }, location.origin);
  try {
    const result = await chrome.runtime.sendMessage({
      type: runtimeType,
      url: data.url,
      jobId: data.jobId || ""
    });
    window.postMessage({
      source: "comic-download-extension",
      requestId: data.requestId,
      ok: Boolean(result?.ok),
      error: result?.error || "",
      preview: result?.preview || null,
      extension_version: result?.extension_version || ""
    }, location.origin);
  } catch (error) {
    window.postMessage({ source: "comic-download-extension", requestId: data.requestId, ok: false, error: error.message }, location.origin);
  }
});
