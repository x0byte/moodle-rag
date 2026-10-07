// Downloads a scanned course's files with the browser's Moodle session and
// posts them to the local ingest server. Needs scan.js loaded first.

globalThis.MoodleSync = (() => {
  const SERVER = "http://127.0.0.1:8765";
  const MOODLE_HOST = "learning.monash.edu";
  const CONCURRENCY = 3;

  // Monash's Moodle redirects file downloads to signed URLs on this host. It
  // must also be listed under host_permissions in manifest.json.
  const FILE_HOSTS = new Set(["d25zr1xy094zys.cloudfront.net"]);

  // Activity types that are never downloaded.
  const SKIPPED_MODULES = {
    url: "external link",
    lti: "external tool",
    folder: "folders not supported yet"
  };

  // Moodle file icon name -> file type. A file is ruled out before any request
  // only if that type is missing from the server's supported types (/health).
  const ICON_TYPES = {
    pdf: "pdf",
    powerpoint: "pptx",
    document: "docx",
    spreadsheet: "xlsx",
    archive: "zip",
    video: "video", mpeg: "video", quicktime: "video", avi: "video", flash: "video",
    audio: "audio", mp3: "audio", wav: "audio",
    image: "image", jpeg: "image", png: "image", gif: "image", bmp: "image"
  };

  const MIME_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/html": "html"
  };

  const VIDEO_EXTENSIONS = new Set(["mp4", "m4v", "mov", "webm", "mkv", "avi", "wmv"]);

  // Server statuses that mean the file has been dealt with.
  const DONE = new Set(["ok", "no_text"]);

  async function serverInfo() {
    let response;
    try {
      response = await fetch(`${SERVER}/health`);
    } catch {
      throw new Error("Ingest server is not running. Start it with: uv run moodle-ingest");
    }
    if (!response.ok) {
      throw new Error(`Ingest server returned HTTP ${response.status}.`);
    }
    return response.json();
  }

  async function knownResources(urls) {
    const response = await fetch(`${SERVER}/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls })
    });
    if (!response.ok) {
      throw new Error(`Ingest server returned HTTP ${response.status}.`);
    }
    return (await response.json()).known;
  }

  function fileType(name, contentType) {
    const mime = (contentType || "").split(";")[0].trim().toLowerCase();
    let extension = name.match(/\.([a-z0-9]{1,5})$/i)?.[1].toLowerCase();
    if (extension === "htm") {
      extension = "html";
    }
    if (extension && extension !== "php") {
      return VIDEO_EXTENSIONS.has(extension) ? "video" : extension;
    }
    return MIME_TYPES[mime] || mime.split("/")[0];
  }

  function decode(value) {
    try {
      return decodeURIComponent(value);
    } catch {
      return value;
    }
  }

  // What a download response is, read from its URL and headers only.
  //
  // The file host's URL path is the file's content hash and carries no name,
  // so the name and type come from the response-content-* parameters Moodle
  // signs into it. `resolved` drops the query string (signature and expiry):
  // what is left changes exactly when the file does, whether it is a content
  // hash or a pluginfile.php path with a revision number.
  function describe(response) {
    const url = new URL(response.url);
    const disposition =
      url.searchParams.get("response-content-disposition") ||
      response.headers.get("Content-Disposition") ||
      "";
    const name = decode(
      disposition.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i)?.[1] ||
      url.pathname.split("/").pop()
    );
    const contentType =
      url.searchParams.get("response-content-type") || response.headers.get("Content-Type");
    return {
      name,
      type: fileType(name, contentType),
      resolved: url.origin + url.pathname,
      onMoodle: url.hostname === MOODLE_HOST
    };
  }

  // The response resolves as soon as the headers arrive; the body is only
  // downloaded if the caller reads it, and is cancelled by aborting.
  async function fetchMoodle(url, signal) {
    let response;
    try {
      response = await fetch(url, { credentials: "include", redirect: "follow", signal });
    } catch {
      // A redirect to a host outside host_permissions (single sign-on, or a
      // file host not listed in FILE_HOSTS) is blocked by the browser.
      throw new Error("download blocked (not logged in to Moodle, or unknown file host)");
    }
    const final = new URL(response.url);
    if (final.hostname === MOODLE_HOST && final.pathname.startsWith("/login/")) {
      throw new Error("not logged in to Moodle");
    }
    if (final.hostname !== MOODLE_HOST && !FILE_HOSTS.has(final.hostname)) {
      throw new Error(`unexpected host ${final.hostname}`);
    }
    if (!response.ok) {
      throw new Error(`Moodle returned HTTP ${response.status}`);
    }
    return response;
  }

  // Some resources open in an embedded viewer page instead of redirecting to the file.
  function findFileLink(html, pageUrl) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const element = doc.querySelector(
      '.resourceworkaround a[href*="pluginfile.php"], object[data*="pluginfile.php"],' +
      ' iframe[src*="pluginfile.php"], embed[src*="pluginfile.php"],' +
      ' source[src*="pluginfile.php"], #region-main a[href*="pluginfile.php"]'
    );
    const link =
      element?.getAttribute("href") || element?.getAttribute("data") || element?.getAttribute("src");
    return link ? new URL(link, pageUrl).href : null;
  }

  async function upload(blob, name, resource, course, resolvedUrl) {
    const form = new FormData();
    form.append("file", blob, name);
    form.append("source_url", resource.url);
    if (resolvedUrl) {
      form.append("resolved_url", resolvedUrl);
    }
    form.append("unit_code", course.unitCode);
    form.append("unit_name", course.unitName);
    form.append("section", resource.section);
    form.append("title", resource.title);
    form.append("resource_type", resource.modname);
    form.append("scope", resource.scope);

    const response = await fetch(`${SERVER}/ingest`, { method: "POST", body: form });
    if (!response.ok) {
      throw new Error(`ingest server returned HTTP ${response.status}`);
    }
    return response.json();
  }

  function outcome(result) {
    if (result.status === "error") {
      return { state: "failed", detail: result.detail };
    }
    if (result.status === "unsupported") {
      return { state: "skipped", detail: result.detail };
    }
    if (result.status === "no_text") {
      return { state: "notext", detail: "no text extracted (scanned?)" };
    }
    if (result.action === "linked") {
      return { state: "linked", detail: `same file as ${result.also_in.join(", ")}` };
    }
    if (result.action === "unchanged") {
      return { state: "current", detail: "up to date" };
    }
    const verb = result.action === "updated" ? "updated" : "ingested";
    return { state: "ingested", detail: `${verb}, ${result.pages} pages` };
  }

  async function fetchDocument(url, signal) {
    const response = await fetchMoodle(url, signal);
    return new DOMParser().parseFromString(await response.text(), "text/html");
  }

  // Inline section notes and Moodle "page" activities are sent as small HTML
  // documents. They have no file URL to compare, so they are always sent and
  // the server's content hash decides whether anything changed.
  async function syncHtml(resource, context, report, signal) {
    if (!context.supported.has("html")) {
      return { state: "skipped", detail: "html not supported" };
    }
    let html = resource.html;
    if (!html) {
      report({ state: "downloading", detail: "downloading" });
      const doc = await fetchDocument(resource.url, signal);
      html = MoodleScan.pageContent(doc, resource.url, resource.title);
    }
    const blob = new Blob([html], { type: "text/html" });
    return outcome(await upload(blob, `${resource.title}.html`, resource, context.course, null));
  }

  // Reads every section page of the course (from scan.sectionPages) and adds
  // what is on them to the scan: inline text, and files linked from inside it
  // that the course index does not list. onProgress(done, total).
  async function crawlSections(scan, onProgress = () => {}) {
    const pages = scan.sectionPages || [];
    const results = new Array(pages.length);
    scan.pageErrors = [];
    let next = 0;
    let done = 0;
    async function worker() {
      while (next < pages.length) {
        const index = next++;
        try {
          const doc = await fetchDocument(pages[index]);
          results[index] = MoodleScan.scanPage(doc, pages[index], scan.sectionPaths);
        } catch (error) {
          // A page that could not be read is left out of the scan's scopes,
          // so nothing found on it earlier is treated as removed.
          scan.pageErrors.push(`${pages[index]}: ${error.message}`);
        }
        onProgress(++done, pages.length);
      }
    }
    await Promise.all(Array.from({ length: CONCURRENCY }, worker));
    // Merged in page order, so the result does not depend on download timing.
    for (const page of results) {
      if (page) {
        MoodleScan.mergePage(scan, page);
      }
    }
    return scan;
  }

  // Decides what a resource is before downloading it: activity type, then the
  // file icon, then the resolved URL and Content-Type from the response headers.
  async function syncResource(resource, context, report) {
    if (SKIPPED_MODULES[resource.modname]) {
      return { state: "skipped", detail: SKIPPED_MODULES[resource.modname] };
    }
    if (!["resource", "file", "inline", "page"].includes(resource.modname)) {
      return { state: "skipped", detail: "video or interactive activity" };
    }
    const hinted = ICON_TYPES[resource.iconHint];
    if (hinted && !context.supported.has(hinted)) {
      return { state: "skipped", detail: `${hinted} not supported` };
    }

    const controller = new AbortController();
    try {
      if (resource.modname === "inline" || resource.modname === "page") {
        return await syncHtml(resource, context, report, controller.signal);
      }
      report({ state: "checking", detail: "checking" });
      // redirect=1 asks Moodle to send the file even when the resource is set to embed.
      const firstUrl =
        resource.modname === "resource" ? `${resource.url}&redirect=1` : resource.url;
      let response = await fetchMoodle(firstUrl, controller.signal);
      let file = describe(response);

      // An HTML page on Moodle itself is the embedded viewer, not the file.
      if (file.type === "html" && file.onMoodle && !response.url.includes("/pluginfile.php/")) {
        const link = findFileLink(await response.text(), response.url);
        if (!link) {
          return { state: "skipped", detail: "no downloadable file" };
        }
        response = await fetchMoodle(link, controller.signal);
        file = describe(response);
      }

      if (!context.supported.has(file.type)) {
        return { state: "skipped", detail: `${file.type || "unknown type"} not supported` };
      }

      // An unchanged resolved URL means an unchanged file (see describe).
      // Otherwise the server compares content hashes.
      const known = context.known[resource.url];
      if (known && known.resolved_url === file.resolved && DONE.has(known.status)) {
        return { state: "current", detail: "up to date" };
      }

      report({ state: "downloading", detail: "downloading" });
      const blob = await response.blob();
      const name = /\.[a-z0-9]{1,5}$/i.test(file.name) ? file.name : `${resource.title}.${file.type}`;
      return outcome(await upload(blob, name, resource, context.course, file.resolved));
    } catch (error) {
      return { state: "failed", detail: error.message };
    } finally {
      controller.abort(); // cancels the download if the body was never read
    }
  }

  // Runs the sync. onUpdate(index, {state, detail}) is called as each resource progresses.
  async function syncCourse(scan, onUpdate) {
    const info = await serverInfo();
    const context = {
      course: scan.course,
      supported: new Set(info.supported_types),
      known: await knownResources(scan.resources.map((resource) => resource.url))
    };

    const counts = {};
    const notIngested = { skipped: [], failed: [] };
    let next = 0;
    async function worker() {
      while (next < scan.resources.length) {
        const index = next++;
        const result = await syncResource(scan.resources[index], context, (update) =>
          onUpdate(index, update, false)
        );
        counts[result.state] = (counts[result.state] || 0) + 1;
        notIngested[result.state]?.push({
          title: scan.resources[index].title,
          section: scan.resources[index].section,
          url: scan.resources[index].url,
          reason: result.detail
        });
        onUpdate(index, result, true);
      }
    }
    await Promise.all(Array.from({ length: CONCURRENCY }, worker));

    // Tell the server what was not sent (for its log) and what this scan
    // covered, so it can drop resources that have been removed from Moodle.
    // Resources outside the scanned scopes are left alone.
    const response = await fetch(`${SERVER}/sync/finish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        unit_code: scan.course.unitCode,
        scopes: scan.scopes,
        seen: scan.resources.map((resource) => resource.url),
        counts,
        ...notIngested
      })
    });
    if (response.ok) {
      const removed = (await response.json()).removed.length;
      if (removed) {
        counts.removed = removed;
      }
    }
    return counts;
  }

  return { syncCourse, crawlSections, serverInfo };
})();
