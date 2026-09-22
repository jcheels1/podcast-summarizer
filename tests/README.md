# Tests

Plain scripts, no test runner needed:

```
.venv/Scripts/python.exe tests/test_spotify_oauth.py
.venv/Scripts/python.exe tests/test_feed_matching.py
.venv/Scripts/python.exe tests/test_directory.py
.venv/Scripts/python.exe tests/test_config_memory.py
```

Each prints a PASS/FAIL line per assertion and exits non-zero if any fail.

Both stub out the network deliberately — not only for speed, but because
this project is developed on a machine where outbound HTTPS to several
hosts (`api.spotify.com` and `itunes.apple.com` among them) dies at the
request stage. The parts worth testing here are ours anyway: name
normalization, match scoring and its refusal to over-confirm, OAuth state
handling, token refresh, and pagination.
