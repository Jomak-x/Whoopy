# Local API

The future FastAPI transport, local queue tasks, and progress reporting belong
here. Phase 1's transport-independent `LocalControlPlane` lives in
`whoopy/control.py`; future routes should call it or its successor rather than
duplicating run creation. No ML model should run inside the API process.
