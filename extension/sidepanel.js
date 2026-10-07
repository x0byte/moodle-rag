const MOODLE_ORIGIN = "https://learning.monash.edu/";

const syncButton = document.getElementById("syncButton");
const scanButton = document.getElementById("scanButton");
const status = document.getElementById("status");
const progress = document.getElementById("progress");

const results = document.getElementById("results");

const courseTitle = document.getElementById("courseTitle");
const summary = document.getElementById("summary");

const resourceList = document.getElementById("resourceList");

const STATE_LABELS = {
  queued: "queued",
  checking: "checking",
  downloading: "downloading",
  ingested: "ingested",
  linked: "linked",
  current: "up to date",
  skipped: "skipped",
  notext: "no text",
  failed: "failed",
  removed: "removed from Moodle"
};

// Order of the counts in the end-of-sync summary.
const SUMMARY_STATES = ["ingested", "linked", "current", "skipped", "notext", "failed", "removed"];

async function scanTab() {

  const [tab] = await chrome.tabs.query({
    active: true,
    currentWindow: true
  });

  if (!tab?.id || !tab.url?.startsWith(MOODLE_ORIGIN)) {
    throw new Error("Open a Monash Moodle course page first, then try again.");
  }

  const message = {
    type: "SCAN_MOODLE_PAGE"
  };

  let response;

  try {
    response = await chrome.tabs.sendMessage(tab.id, message);
  } catch {
    // The tab was open before the extension was loaded, so it has no content script yet.
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["scan.js", "content.js"]
    });
    response = await chrome.tabs.sendMessage(tab.id, message);
  }

  if (!response?.success) {
    throw new Error(response?.error || "Scan failed.");
  }

  // With a course index, the scan lists the section pages; read them for
  // inline text and for files the index does not list.
  const data = await MoodleSync.crawlSections(response.data, (done, total) => {
    status.innerText = `Reading section pages: ${done} / ${total}`;
  });

  if (!data.resources.length) {
    throw new Error("No course material found on this page. Open the course's main page.");
  }

  return data;
}

function render(data) {

  courseTitle.innerText = `${data.course.unitCode} ${data.course.unitName}`;

  summary.innerText =
    `${data.resources.length} resources, ${data.ignored} other activities ignored` +
    (data.wholeCourse ? "" : " (this page only: no course index found)") +
    (data.pageErrors?.length ? `, ${data.pageErrors.length} section pages could not be read` : "");

  resourceList.innerHTML = "";

  const rows = [];

  for (const resource of data.resources) {

    const item = document.createElement("div");

    item.className = "resource";

    const name = document.createElement("div");

    name.className = "resource-name";
    name.innerText = resource.title;

    const section = document.createElement("div");

    section.className = "resource-section";
    section.innerText = resource.section || "No section";

    const state = document.createElement("div");

    state.className = "resource-state";

    item.appendChild(name);
    item.appendChild(section);
    item.appendChild(state);

    resourceList.appendChild(item);

    rows.push({ item, state });

  }

  results.hidden = false;

  return rows;
}

function setRowState(row, update) {
  row.item.dataset.state = update.state;
  const label = STATE_LABELS[update.state] || update.state;
  const detail = update.detail && update.detail !== label ? `: ${update.detail}` : "";
  row.state.innerText = `${label}${detail}`;
}

function setBusy(busy) {
  syncButton.disabled = busy;
  scanButton.disabled = busy;
}

scanButton.addEventListener("click", async () => {

  status.innerText = "Scanning Moodle...";
  progress.hidden = true;
  results.hidden = true;
  setBusy(true);

  try {
    render(await scanTab());
    status.innerText = "Page scanned.";
  } catch (error) {
    console.error(error);
    status.innerText = error.message;
  } finally {
    setBusy(false);
  }

});

syncButton.addEventListener("click", async () => {

  status.innerText = "Scanning Moodle...";
  progress.hidden = true;
  results.hidden = true;
  setBusy(true);

  try {

    // Fail early, before scanning, if the server is not there.
    await MoodleSync.serverInfo();

    const data = await scanTab();
    const rows = render(data);

    for (const row of rows) {
      setRowState(row, { state: "queued" });
    }

    let done = 0;

    progress.max = rows.length;
    progress.value = 0;
    progress.hidden = false;
    status.innerText = `Syncing ${data.course.unitCode}: 0 / ${rows.length}`;

    const counts = await MoodleSync.syncCourse(data, (index, update, finished) => {
      setRowState(rows[index], update);
      if (finished) {
        done += 1;
        progress.value = done;
        status.innerText = `Syncing ${data.course.unitCode}: ${done} / ${rows.length}`;
      }
    });

    status.innerText =
      `Sync finished: ` +
      SUMMARY_STATES
        .filter((state) => counts[state])
        .map((state) => `${counts[state]} ${STATE_LABELS[state]}`)
        .join(", ");

  } catch (error) {
    console.error(error);
    status.innerText = error.message;
  } finally {
    setBusy(false);
  }

});
