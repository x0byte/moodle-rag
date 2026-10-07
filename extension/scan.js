// Finds the course material on a Moodle course page.
//
// Takes a Document and its URL rather than using the globals, so the same code
// can scan the live page (content script) and pages fetched by the side panel.
//
// scanCourse reads the course index: every section and activity in the unit.
// scanPage reads one page's content: its activities, files linked from inside
// text, and the inline text itself (labels and content blocks), which becomes
// one "section notes" document per section.

globalThis.MoodleScan = globalThis.MoodleScan || (() => {
  const UNIT_CODE = /\b[A-Z]{3}\d{4}\b/;

  // Activity types that hold course material. Everything else (forums,
  // quizzes, assignments, ...) is counted but not listed.
  const CONTENT_MODULES = new Set(["resource", "folder", "page", "url"]);

  // Video and interactive activities: listed so the panel can show them as skipped.
  const MEDIA_MODULES = new Set([
    "lti", "hvp", "h5pactivity", "scorm", "kalvidres", "panopto", "videotime", "echo360"
  ]);

  const SECTION = '[data-for="section"], li.section, .course-section';
  const SECTION_TITLE = '[data-for="section_title"], .sectionname, h3, h4';
  const ACTIVITY = 'li.activity, [data-for="cmitem"]';

  // The course index (the sidebar tree) lists every section and activity in
  // the course, including sections that are not on the current page.
  const INDEX = "#courseindex, .courseindex";
  const INDEX_SECTION = '[data-for="section"]';

  // Where an activity's inline text lives: the whole of a label or content
  // block, or the description under a linked activity.
  const INLINE_CONTENT = ".activity-altcontent, .activity-description";
  const NOT_CONTENT =
    "script, style, noscript, template, svg, iframe, object, form, button, input, select," +
    " textarea, .accesshide, .sr-only, .visually-hidden";

  // Section notes shorter than this are headings with nothing under them.
  const MIN_INLINE_CHARS = 80;

  function cleanText(value) {
    return value?.replace(/\s+/g, " ").trim() || "";
  }

  // textContent, not innerText: fetched documents are never rendered.
  function textWithoutHidden(element) {
    if (!element) {
      return "";
    }
    const clone = element.cloneNode(true);
    clone.querySelectorAll(".accesshide, .sr-only, .visually-hidden").forEach((hidden) => hidden.remove());
    return cleanText(clone.textContent);
  }

  function textOf(element) {
    return cleanText(element?.textContent);
  }

  function absoluteUrl(href, pageUrl) {
    try {
      const url = new URL(href, pageUrl);
      url.hash = "";
      return url.href;
    } catch {
      return null;
    }
  }

  function moduleOf(url) {
    return url.match(/\/mod\/([a-z0-9_]+)\//)?.[1] || null;
  }

  function sectionTitle(element, fallback) {
    const section = element.closest(SECTION);
    if (!section) {
      return fallback;
    }
    return (
      cleanText(section.getAttribute("data-sectionname")) ||
      textWithoutHidden(section.querySelector(SECTION_TITLE)) ||
      fallback
    );
  }

  // "Learning › Week 1 - Chapter 1 › Own-time" for a nested index section.
  function indexSectionPath(section) {
    const titles = [];
    for (let node = section; node; node = node.parentElement?.closest(INDEX_SECTION)) {
      const item = [...node.children].find((child) => child.matches('[data-for="section_item"]'));
      const title = textOf(item?.querySelector('[data-for="section_title"]'));
      if (title) {
        titles.unshift(title);
      }
    }
    return titles.join(" › ");
  }

  // Moodle's file icons are named after the file type: .../f/pdf, .../f/video.
  function iconHint(element) {
    for (const image of element.querySelectorAll("img")) {
      const match = (image.getAttribute("src") || "").match(/\/f\/([a-z0-9]+)/i);
      if (match) {
        return match[1].toLowerCase();
      }
    }
    return null;
  }

  function mainRegion(doc) {
    return (
      doc.querySelector("#region-main") ||
      doc.querySelector('[role="main"]') ||
      doc.querySelector("main") ||
      doc.body
    );
  }

  function scopeOf(pageUrl) {
    const page = new URL(pageUrl);
    return `page:${page.pathname}${page.search}`;
  }

  function escapeHtml(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // The element's content as plain markup: no scripts, styles or controls, and
  // no attributes except absolute link targets. The same content always gives
  // the same bytes, so the server can tell by hash whether it changed.
  function cleanHtml(element, pageUrl) {
    const clone = element.cloneNode(true);
    clone.querySelectorAll(NOT_CONTENT).forEach((node) => node.remove());
    for (const node of clone.querySelectorAll("*")) {
      const href = node.tagName === "A" ? absoluteUrl(node.getAttribute("href") || "", pageUrl) : null;
      for (const attribute of [...node.attributes]) {
        node.removeAttribute(attribute.name);
      }
      if (href) {
        node.setAttribute("href", href);
      }
    }
    return clone.innerHTML.trim();
  }

  function htmlDocument(title, bodyHtml) {
    return (
      `<!DOCTYPE html><html><head><meta charset="utf-8"><title>${escapeHtml(title)}</title></head>` +
      `<body><h1>${escapeHtml(title)}</h1>${bodyHtml}</body></html>`
    );
  }

  function activityTitle(item, link) {
    return (
      cleanText(item.querySelector("[data-activityname]")?.getAttribute("data-activityname")) ||
      textWithoutHidden(item.querySelector(".instancename")) ||
      textWithoutHidden(link)
    );
  }

  // Scans one page's content. sectionPaths maps a section number to its full
  // path in the course index, when there is one.
  function scanPage(doc, pageUrl, sectionPaths = {}) {
    const main = mainRegion(doc);
    const scope = scopeOf(pageUrl);
    const resources = [];
    const ignoredUrls = [];
    const seen = new Set();

    function sectionNumber(section) {
      return section?.id.match(/^section-(\d+)$/)?.[1] || section?.getAttribute("data-number");
    }

    function sectionPath(element, fallback) {
      return sectionPaths[sectionNumber(element.closest(SECTION))] || sectionTitle(element, fallback);
    }

    function add(url, fields) {
      if (!url || seen.has(url)) {
        return;
      }
      seen.add(url);
      resources.push({ url, scope, iconHint: null, ...fields });
    }

    // 1. Activities, as laid out by the standard course formats.
    for (const item of main.querySelectorAll(ACTIVITY)) {
      const link = item.querySelector('a[href*="/mod/"]');
      const url = link && absoluteUrl(link.getAttribute("href"), pageUrl);
      if (!url || seen.has(url)) {
        continue; // labels, and activities the student cannot open yet
      }
      const modname = item.className.match(/\bmodtype_([a-z0-9_]+)/)?.[1] || moduleOf(url);
      if (!CONTENT_MODULES.has(modname) && !MEDIA_MODULES.has(modname)) {
        ignoredUrls.push(url);
        seen.add(url);
        continue;
      }
      add(url, {
        modname,
        title: activityTitle(item, link),
        section: sectionPath(item, ""),
        iconHint: iconHint(item)
      });
    }

    // 2. Anything the activity list missed: files linked from inside labels,
    //    content blocks and section summaries, and course formats without the
    //    usual markup. The section falls back to the nearest heading above.
    let lastHeading = "";
    for (const element of main.querySelectorAll("h2, h3, h4, a[href]")) {
      if (element.tagName !== "A") {
        lastHeading = textWithoutHidden(element) || lastHeading;
        continue;
      }
      const url = absoluteUrl(element.getAttribute("href"), pageUrl);
      if (!url || seen.has(url)) {
        continue;
      }
      const isFile = url.includes("/pluginfile.php/");
      const modname = isFile ? "file" : moduleOf(url);
      if (!isFile && !(/\/mod\/[a-z0-9_]+\/view\.php/.test(url) && CONTENT_MODULES.has(modname))) {
        continue;
      }
      const title = textWithoutHidden(element);
      if (!title) {
        continue;
      }
      add(url, {
        modname,
        title,
        section: sectionPath(element, lastHeading),
        iconHint: iconHint(element)
      });
    }

    // 3. Inline text: one document per section, in page order.
    main.querySelectorAll(SECTION).forEach((section, position) => {
      const parts = [];
      for (const item of section.querySelectorAll(ACTIVITY)) {
        if (item.closest(SECTION) !== section || !/\bmodtype_/.test(item.className)) {
          continue; // a nested section's activity, or a dashboard widget
        }
        const content = item.querySelector(INLINE_CONTENT);
        const html = content ? cleanHtml(content, pageUrl) : "";
        if (!html) {
          continue;
        }
        // A linked activity's description needs its title; a label is its own text.
        const link = item.querySelector('.activityname a[href*="/mod/"]');
        parts.push(link ? `<h3>${escapeHtml(activityTitle(item, link))}</h3>${html}` : html);
      }
      const body = parts.map((part) => `<div>${part}</div>`).join("\n");
      if (cleanText(body.replace(/<[^>]+>/g, " ")).length < MIN_INLINE_CHARS) {
        return;
      }
      const path = sectionPath(section, "") || `Section ${position + 1}`;
      const title = `Section notes: ${path.split(" › ").slice(-2).join(" › ")}`;
      const anchor = section.id || `section-${sectionNumber(section) ?? position}`;
      add(`${absoluteUrl(pageUrl, pageUrl)}#${anchor}`, {
        modname: "inline",
        title,
        section: path,
        html: htmlDocument(title, body)
      });
    });

    return { scope, resources, ignoredUrls };
  }

  // The content of a Moodle "page" activity, as a standalone HTML document.
  function pageContent(doc, pageUrl, title) {
    const main = mainRegion(doc);
    const content =
      main.querySelector(".generalbox") || main.querySelector('[role="main"]') || main;
    return htmlDocument(title, cleanHtml(content, pageUrl));
  }

  // Adds a scanned page to a course scan. Whatever is already there wins.
  function mergePage(scan, page) {
    const known = new Set(scan.resources.map((resource) => resource.url));
    const ignored = new Set(scan.ignoredUrls);
    for (const resource of page.resources) {
      if (!known.has(resource.url) && !ignored.has(resource.url)) {
        known.add(resource.url);
        scan.resources.push(resource);
      }
    }
    for (const url of page.ignoredUrls) {
      if (!known.has(url)) {
        ignored.add(url);
      }
    }
    scan.ignoredUrls = [...ignored];
    scan.ignored = ignored.size;
    if (!scan.scopes.includes(page.scope)) {
      scan.scopes.push(page.scope);
    }
    return scan;
  }

  function scanCourse(doc, pageUrl) {
    const pageTitle = cleanText(doc.title);
    const heading = textWithoutHidden(doc.querySelector("h1")) || pageTitle;
    const courseId =
      doc.body?.className.match(/\bcourse-(\d+)\b/)?.[1] ||
      (/\/course\//.test(pageUrl) ? new URL(pageUrl).searchParams.get("id") : null);
    const unitCode =
      (heading.match(UNIT_CODE) || pageTitle.match(UNIT_CODE))?.[0] ||
      (courseId ? `COURSE${courseId}` : "UNKNOWN");
    const unitName = cleanText(heading.replace(unitCode, "").replace(/^[\s:\-–|]+/, "")) || heading;

    const scan = {
      course: { id: courseId, unitCode, unitName, title: heading, url: pageUrl },
      resources: [],
      ignoredUrls: [],
      ignored: 0,
      // Where resources were found: "index", or "page:<path>" per page scanned.
      // A later sync may only treat a resource as gone if it scanned the same scope.
      scopes: [],
      // From the course index: each section's full path by number, and the
      // pages that between them show every section.
      sectionPaths: {},
      sectionPages: [],
      // False when there was no course index, so only this page was scanned.
      wholeCourse: false
    };

    const seen = new Set();
    const ignored = new Set();
    const pages = new Set();
    const index = doc.querySelector(INDEX);
    for (const section of index?.querySelectorAll(INDEX_SECTION) || []) {
      const path = indexSectionPath(section);
      const item = [...section.children].find((child) => child.matches('[data-for="section_item"]'));
      const pageLink = item?.querySelector('[data-for="section_title"]')?.getAttribute("href");
      const sectionPage = pageLink && absoluteUrl(pageLink, pageUrl);
      if (sectionPage?.includes("/course/")) {
        pages.add(sectionPage);
      }
      if (section.getAttribute("data-number") !== null) {
        scan.sectionPaths[section.getAttribute("data-number")] = path;
      }
      for (const cm of section.querySelectorAll('[data-for="cm"]')) {
        if (cm.closest(INDEX_SECTION) !== section) {
          continue; // belongs to a nested section, handled on its own turn
        }
        const link = cm.querySelector('a[href*="/mod/"]');
        const url = link && absoluteUrl(link.getAttribute("href"), pageUrl);
        if (!url || seen.has(url)) {
          continue; // inline content with no page of its own
        }
        seen.add(url);
        const modname = moduleOf(url);
        if (!CONTENT_MODULES.has(modname) && !MEDIA_MODULES.has(modname)) {
          ignored.add(url);
          continue;
        }
        scan.resources.push({
          url, modname, title: textOf(link), section: path, iconHint: null, scope: "index"
        });
      }
    }

    if (scan.resources.length || pages.size) {
      // The section pages are read afterwards by the side panel (MoodleSync.crawlSections),
      // always from the same fetched HTML, whichever page the sync was started on.
      scan.wholeCourse = true;
      scan.scopes.push("index");
      scan.sectionPages = [...pages];
      scan.ignoredUrls = [...ignored];
      scan.ignored = ignored.size;
      return scan;
    }

    return mergePage(scan, scanPage(doc, pageUrl));
  }

  return { scanCourse, scanPage, mergePage, pageContent };
})();
