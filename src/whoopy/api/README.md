# Local API

The versioned loopback FastAPI transport lives in `whoopy.webui.api`, with its
durable application boundary in `whoopy.webui.server`. Routes delegate run
creation, admission, recovery, revisions, exports, and model operations to
those boundaries instead of duplicating pipeline rules. Model inference runs
in supervised workers rather than request handlers.
