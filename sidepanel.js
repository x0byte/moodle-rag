const scanButton = document.getElementById("scanButton");
const status = document.getElementById("status");

const results = document.getElementById("results");

const courseTitle = document.getElementById("courseTitle");
const headingCount = document.getElementById("headingCount");
const resourceCount = document.getElementById("resourceCount");
const fileCount = document.getElementById("fileCount");

const resourceList = document.getElementById("resourceList");

scanButton.addEventListener("click", async () => {

  status.innerText = "Scanning Moodle...";

  results.hidden = true;

  try {

    const [tab] = await chrome.tabs.query({
      active: true,
      currentWindow: true
    });

    if (!tab?.id) {
      throw new Error("Couldn't find the current tab.");
    }

    const response = await chrome.tabs.sendMessage(
      tab.id,
      {
        type: "SCAN_MOODLE_PAGE"
      }
    );

    if (!response?.success) {
      throw new Error(response?.error || "Scan failed.");
    }

    render(response.data);

  } catch (error) {

    console.error(error);

    status.innerText =
      "Open a Monash Moodle page first, then try again.";

  }

});

function render(data) {

  status.innerText = "Page scanned successfully.";

  courseTitle.innerText = data.course.title;

  headingCount.innerText = data.headings.length;
  resourceCount.innerText = data.resources.length;
  fileCount.innerText = data.files.length;

  resourceList.innerHTML = "";

  for (const resource of data.resources.slice(0, 30)) {

    const item = document.createElement("div");

    item.className = "resource";

    const name = document.createElement("div");

    name.className = "resource-name";
    name.innerText = resource.text;

    const url = document.createElement("div");

    url.className = "resource-url";
    url.innerText = resource.url;

    item.appendChild(name);
    item.appendChild(url);

    resourceList.appendChild(item);

  }

  results.hidden = false;
}