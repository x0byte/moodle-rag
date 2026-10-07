// Guarded so a second injection (see scanTab in sidepanel.js) does not add a second listener.
if (!globalThis.moodleFinderListening) {
  globalThis.moodleFinderListening = true;

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type !== "SCAN_MOODLE_PAGE") {
      return;
    }

    try {
      sendResponse({
        success: true,
        data: MoodleScan.scanCourse(document, window.location.href)
      });
    } catch (error) {
      sendResponse({
        success: false,
        error: error.message
      });
    }
  });
}
