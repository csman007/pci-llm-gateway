---
name: postman-sync-on-route-change
enabled: true
event: file
action: warn
conditions:
  - field: file_path
    operator: regex_match
    pattern: services/api-gateway/routes/.*\.py$|services/api-gateway/schemas/.*\.py$
---

**Route or schema file modified — update `postman_collection.json`!**

A route or schema file was changed. Before finishing, sync the Postman collection:

**If a new endpoint was added:**
- Add a new request item inside the correct folder (`Auth`, `Inference`, `Agent`, `RAG`, or a new folder).
- Include all required headers: `Authorization: Bearer {{jwt_token}}`, `Content-Type: application/json`, `x-api-key: {{api_key}}`.
- Add at least one happy-path example and one failure-path example (e.g. `No Auth (401)`).

**If an endpoint was modified:**
- Update the request body `raw` JSON to reflect new or changed fields.
- Update any request/response field names that changed in the schema.
- If the path changed, update the `url` field.

**If an endpoint was removed:**
- Delete the corresponding item from `postman_collection.json`.

**Collection location:** `postman_collection.json` at the project root.
**Format:** Postman Collection v2.1 — keep the `{{base_url}}`, `{{jwt_token}}`, and `{{api_key}}` variables.