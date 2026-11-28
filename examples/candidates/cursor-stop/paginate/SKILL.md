---
name: paginate
description: Read the requested pages using the continuation cursor and finish in the requested format.
---

For a first-page request, finish after reading page 1.

For an all-records request, read another page only when the last successful result supplies a non-null next_cursor. Pass that exact cursor to the tool. A null cursor ends pagination. If a cursor fails or repeats, stop and report the incomplete read.

Collect each requested record ID once. Return the collected IDs in the user's requested format. When the user requests a JSON object, output just the object.
