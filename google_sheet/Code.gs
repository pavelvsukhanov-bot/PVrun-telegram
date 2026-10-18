// PVrun run archive: Apps Script bound to the private Google Sheet.
// The bot (GitHub Actions, archive.py) calls this web app; TOKEN keeps anyone
// else out, so the sheet itself stays private.
//
// Install: Extensions → Apps Script → paste this file → set TOKEN → Run "setup" once
// → Deploy → New deployment → Web app (Execute as: Me, Who has access: Anyone).

const TOKEN = 'PASTE_ARCHIVE_TOKEN_HERE';
const SHEET_NAME = 'Пробежки';
// ID, date, start time and pace are kept as text: no locale-dependent date or time parsing
const TEXT_COLUMNS = ['ID', 'Дата', 'Старт', 'UTC-смещение', 'Темп /км'];

function sheet_() {
  const ss = SpreadsheetApp.getActive();
  return ss.getSheetByName(SHEET_NAME) || ss.getSheets()[0];
}

function headers_(sh) {
  return sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
}

function textColumns_(sh, headers) {
  TEXT_COLUMNS.forEach(name => {
    const col = headers.indexOf(name) + 1;
    if (col > 0) sh.getRange(1, col, sh.getMaxRows(), 1).setNumberFormat('@');
  });
}

// Run once from the editor: names the tab and formats the header row.
function setup() {
  const sh = sheet_();
  sh.setName(SHEET_NAME);
  sh.setFrozenRows(1);
  const headers = headers_(sh);
  sh.getRange(1, 1, 1, headers.length)
    .setFontWeight('bold').setBackground('#1F3864').setFontColor('#FFFFFF').setWrap(true);
  textColumns_(sh, headers);
  sh.autoResizeColumns(1, headers.length);
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// Upsert by ID: {"token": "...", "rows": [{"ID": "...", "Дата": "...", ...}]}
function doPost(e) {
  const body = JSON.parse(e.postData.contents);
  if (body.token !== TOKEN) return json_({ok: false, error: 'forbidden'});
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const sh = sheet_();
    const headers = headers_(sh);
    textColumns_(sh, headers);
    const n = sh.getLastRow() - 1;
    const ids = n > 0 ? sh.getRange(2, 1, n, 1).getValues().map(r => String(r[0])) : [];
    let added = 0, updated = 0;
    body.rows.forEach(row => {
      const values = headers.map(h => (row[h] === undefined || row[h] === null) ? '' : row[h]);
      const i = ids.indexOf(String(row['ID']));
      if (i >= 0) {
        sh.getRange(i + 2, 1, 1, values.length).setValues([values]);
        updated++;
      } else {
        sh.appendRow(values);
        ids.push(String(row['ID']));
        added++;
      }
    });
    return json_({ok: true, added: added, updated: updated});
  } finally {
    lock.releaseLock();
  }
}

// Read rows: ?token=...&from=YYYY-MM-DD&to=YYYY-MM-DD
function doGet(e) {
  if (e.parameter.token !== TOKEN) return json_({ok: false, error: 'forbidden'});
  const values = sheet_().getDataRange().getValues();
  const headers = values.shift();
  const from = e.parameter.from || '';
  const to = e.parameter.to || '9999';
  const rows = values
    .map(r => Object.fromEntries(headers.map((h, i) => [h, r[i]])))
    .filter(r => String(r['Дата']) >= from && String(r['Дата']) <= to);
  return json_({ok: true, rows: rows});
}
