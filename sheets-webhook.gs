/**
 * Google Apps Script — receives leads from the voice agent and appends them to
 * a sheet. Free, no service account or JSON key needed.
 *
 * SETUP
 *  1. Create a Google Sheet. Name the first tab "Leads".
 *  2. Extensions -> Apps Script. Delete everything, paste this in.
 *  3. Change SHARED_SECRET below to a long random string.
 *  4. Deploy -> New deployment -> type "Web app".
 *       Execute as: Me
 *       Who has access: Anyone
 *  5. Copy the /exec URL it gives you.
 *  6. In Coolify set:
 *       SHEETS_WEBHOOK_URL    = that /exec URL
 *       SHEETS_WEBHOOK_SECRET = the same string as SHARED_SECRET
 *
 * Re-deploy as a NEW VERSION after any edit, or your changes will not be live.
 */

const SHARED_SECRET = "CHANGE-ME-to-a-long-random-string";
const SHEET_NAME = "Leads";

const HEADERS = [
  "Timestamp", "Name", "Email", "Phone", "Company",
  "Problem", "Interest", "Summary", "Session ID", "Visitor ID", "Transcript",
];

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);

    if (body.secret !== SHARED_SECRET) {
      return json({ ok: false, error: "unauthorized" });
    }

    const sheet = getSheet();
    sheet.appendRow([
      new Date(),
      body.name || "",
      body.email || "",
      body.phone || "",
      body.company || "",
      body.problem || "",
      body.interest || "",
      body.summary || "",
      body.session_id || "",
      body.visitor_id || "",
      body.transcript || "",
    ]);

    return json({ ok: true });
  } catch (err) {
    return json({ ok: false, error: String(err) });
  }
}

function getSheet() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = book.getSheetByName(SHEET_NAME);
  if (!sheet) sheet = book.insertSheet(SHEET_NAME);
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

function json(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
