function cleanText(value) {
    return value
      ?.replace(/\s+/g, " ")
      .trim() || "";
  }
  
  function extractPage() {
    const main =
      document.querySelector("main") ||
      document.querySelector('[role="main"]') ||
      document.querySelector("#region-main") ||
      document.body;
  
    const headings = [...main.querySelectorAll("h1, h2, h3, h4")]
      .map((element) => ({
        level: element.tagName.toLowerCase(),
        text: cleanText(element.innerText)
      }))
      .filter((heading) => heading.text);
  
    const links = [...main.querySelectorAll("a[href]")]
      .map((element) => ({
        text: cleanText(element.innerText),
        url: element.href
      }))
      .filter((link) => link.text && link.url);
  
    const files = links.filter((link) => {
      const url = link.url.toLowerCase();
  
      return (
        url.includes("/mod/resource/") ||
        url.includes("pluginfile.php") ||
        /\.(pdf|pptx?|docx?|xlsx?|csv|zip)(\?|$)/i.test(url)
      );
    });
  
    const moodleResources = links.filter((link) => {
      return (
        link.url.includes("/mod/") ||
        link.url.includes("/course/") ||
        link.url.includes("/pluginfile.php")
      );
    });
  
    return {
      page: {
        title: document.title,
        url: window.location.href
      },
  
      course: {
        title:
          cleanText(document.querySelector("h1")?.innerText) ||
          cleanText(document.title)
      },
  
      headings,
  
      files,
  
      resources: moodleResources,
  
      text: cleanText(main.innerText),
  
      capturedAt: new Date().toISOString()
    };
  }
  
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type !== "SCAN_MOODLE_PAGE") {
      return;
    }
  
    try {
      const result = extractPage();
  
      sendResponse({
        success: true,
        data: result
      });
    } catch (error) {
      sendResponse({
        success: false,
        error: error.message
      });
    }
  
    return true;
  });