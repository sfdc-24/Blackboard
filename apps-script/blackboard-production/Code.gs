function sweepDraftsToEndpointV2() {
  const draftsFolderId = "1-WsSPriLK3SXuYwyH04kRZGZD1CFfMoO";
  const blackboardEndpoint = "https://script.google.com/macros/s/AKfycby1JUlXWRzd_28epRKyyWjrU_TrrCWiueRaLATrKgDCgUie93s-1InplCiYWg_1sHbA/exec";
  const blackboardSecret = PropertiesService.getScriptProperties().getProperty("BLACKBOARDSECRET");

  const folder = DriveApp.getFolderById(draftsFolderId);
  const files = folder.getFiles();

  while (files.hasNext()) {
    const file = files.next();
    let content = "";

    // Handles both Google Docs and Plain Text files
    if (file.getMimeType() === MimeType.GOOGLE_DOCS) {
      content = DocumentApp.openById(file.getId()).getBody().getText();
    } else {
      content = file.getBlob().getDataAsString();
    }
    
    const fileName = file.getName();

    // V2 Blackboard Bus Intake Payload
    const v2Payload = {
      secret: blackboardSecret,
      action: "parse_blurb",
      source_tag: "gemini-bridge-voice",
      title: "LIVE_SCRATCHPAD",
      payload: {
        file_name: fileName,
        raw_blurb: content,
        timestamp: new Date().toISOString()
      }
    };

    const options = {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(v2Payload),
      muteHttpExceptions: true
    };

    try {
      const response = UrlFetchApp.fetch(blackboardEndpoint, options);
      Logger.log("Response: " + response.getContentText());
      
      const statusCode = response.getResponseCode();
      Logger.log("Status Code: " + statusCode);
      // Poka-Yoke Safety Check (D-4): Only trash if endpoint succeeds
      if (statusCode >= 200 && statusCode < 300) {
        Logger.log("Response:" + response.getContentText());
        file.setTrashed(true);
      } else {
        Logger.log(`Failed to process ${fileName}. HTTP ${statusCode}: ${response.getContentText()}`);
      }
    } catch (error) {
      Logger.log(`Execution error for ${fileName}: ${error.toString()}`);
    }
  }
}
