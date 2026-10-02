# Uploading to Mediocre Entropia

Run `python skill_tracker_ui.py`, then open **Settings**.

1. Enter the URL of your running service, such as `https://entropia.example.com` or `http://192.168.1.110:8088`. This is the application URL, not its GitHub repository. A trailing `/api` is accepted and normalized.
2. Sign in to your approved server account, generate an API token, and paste it into **API token**.
3. For HTTP on your trusted LAN, explicitly enable **Allow HTTP**. HTTPS is the default. HTTP sends the token without encryption. TLS verification remains enabled and authenticated requests refuse redirects; configure the final service URL.
4. Click **Save settings**, then **Test connection**. Test connection and item/MU uploads need the server's `GET /api/uploads/items` endpoint. Session and skill uploads use the existing API.

**Remember token on this computer** is optional and disabled initially. Remembered tokens are stored without encryption in `server_settings.json`. On POSIX, newly saved settings have mode 0600. Keep this file private; it is ignored by Git. Unchecking the option and saving removes the persisted token. Without remembering, paste the token again after restarting. Settings, receipts, and optional input files are relative to the tracker's working directory, like its other data files.

## Analytic Sessions

The tab lists the local archive used by **What Mob to Hunt**, including start/end times, mob, weapon, PED cycled, loot, notes, and upload receipts. Mark sessions through **Previous Sessions → Add Selected to Mob Analysis**, or remove them there. The list refreshes when opened.

Use **Upload selected** for selected rows on the current page. **Upload all analytic sessions** includes every page. Upload preparation and network requests run in a background thread. Batches respect the server's 100-session, 100,000-event, and 20 MB limits. Each session may have at most 50,000 loot events. Complete the session and assign an ID, mob, and positive PED cycled before uploading. An invalid batch selection is rejected before session writes begin.

The upload sends session import fields and loot receipts, including individual item and event exclusions. It omits local log paths, raw chat history, local backups, and skill snapshots. Naive monitor session clocks are interpreted in the computer's local timezone and converted to UTC; clocks with explicit offsets retain their original instant.

Submission receipts are stored separately in `server_upload_receipts.json`, scoped to the server and a hash of the token. Receipts do not contain the token. The table reports submissions and duplicates; it does not poll moderation status. Another member must approve a submitted session on the server before reports include it. Re-uploading identical data is safe and returns a duplicate. Changing an existing session's content can return HTTP 409; review the existing record on the server rather than automatically changing its ID.

Successful earlier batches keep their receipts if a later batch fails. After a network timeout, check the server or retry: its duplicate checks prevent another copy. HTTP 429 means wait one minute before retrying. Expired/revoked tokens or disabled/unapproved accounts are reported in the status area. Keep the app open until the upload finishes.

## Skills, items, and MU

- **Upload current skills** sends the saved current skill values in memory and replaces your server skill snapshot. Save edited skill values first.
- **Upload items** reads the selected JSON file. Accepts an ItemInput array, `{ "items": [...] }`, or a mapping of item names to details. Only supplied TT/MU fields are updated; existing unspecified fields are preserved. Server versions are fetched just before importing, and concurrent edits cause a conflict rather than being overwritten.
- **Upload MU** sends manual loot MU overrides first, then weekly `market_data.json` values. Percentage and fixed TT+ PED are distinct. It preserves existing server TT fields. New item names use zero TT values until supplied by an item catalog or edited on the server.
- **Upload items + MU** combines the selected catalog with effective local MU; local MU wins for overlapping names.

Example items JSON (PED values; no version needed in the local file):

```json
{
  "items": [
    { "name": "Animal Hide", "value": 0.01, "minTt": 0, "maxTt": 0 },
    { "name": "Example weapon", "minTt": 1, "maxTt": 44, "markupType": "fixed", "markup": 5 }
  ]
}
```

The tracker does not invent per-unit TT values from variable-value loot or send combat equipment statistics as item prices. Supply a catalog with the TT values you want to share. Item/MU uploads change the shared server catalog and are audited there.

Deploy the companion Mediocre Entropia catalog endpoint before testing the connection or uploading items/MU. The endpoint reads only shared catalog metadata and optimistic versions; tokens still cannot read sessions, member skills, reviews, reports, or administrator APIs. No database migration is needed.

## Validation

`python -m unittest discover -s tests -v` covers upload payload privacy, timestamps, exclusions, batching, version/TT preservation, redirects, secret storage, selected/all-page uploads, duplicates, and partial failures. GUI tests require a Tk display.
