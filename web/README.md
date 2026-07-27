# Local Web Application

The first local tester now ships inside `src/whoopy/webui/` so it is included
when the Python package is installed. Start it with:

```bash
uv run --offline whoopy web --open
```

This deliberately small, dependency-free interface exercises the real Phase
3.5 CLI in a child process. It is not a parallel generation implementation.
The server binds only to `127.0.0.1`, keeps task state in memory, and reads
durable results from `runs/`.

The later Phase 4 application may replace this static interface with SvelteKit.
The HTTP boundary and the Python generation pipeline can remain.
