# Pairing your local checkout directly with the agent sandbox

`sync_server.py` is a stdlib-only Git-over-HTTP server that lets your laptop
and the agent sandbox exchange commits **without going through GitHub**.

## Why it goes in this direction

The sandbox cannot dial outward to your machine. Its egress is allowlisted,
verified by probe:

| Host | Result |
| --- | --- |
| `github.com`, `pypi.org`, `files.pythonhosted.org` | HTTP 200 |
| `www.pythonanywhere.com`, `example.com`, `1.1.1.1` | connection fails |

So SSH to your laptop, SSH to PythonAnywhere, and the PythonAnywhere web API
are all unreachable from the sandbox. What *is* reachable is the other
direction: you can reach the sandbox over its preview HTTPS URL. The sync
server uses that.

```
your laptop  --git push-->  preview HTTPS  -->  sync_server  -->  bare repo
                                                                  |
agent working clone  <-------------------------------------------+
```

## Running it

```bash
TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
echo "$TOKEN"     # copy this; it is the password

SYNC_TOKEN="$TOKEN" \
SYNC_REPO=~/arena-sync/hdc.git \
SYNC_SEED_FROM=/path/to/HDC-MAIN \
SYNC_PORT=8000 \
  python3 ops/arena/sync_server.py
```

`SYNC_SEED_FROM` pushes the seed checkout's branches into the bare repo on
first start, so you begin from real code instead of an empty repository. The
seed checkout must not be a shallow clone — the server warns if it is; run
`git fetch --unshallow` first.

## On your machine

```bash
git remote add arena "https://arena:$TOKEN@<port>-<sandbox>.e2b.app/hdc.git"

git push arena HEAD:refs/heads/main   # send your working tree to the agent
git pull arena main                   # take the agent's edits back
```

## Threat model

- Auth is HTTP Basic, username `arena`, password `SYNC_TOKEN`, compared in
  constant time on both halves.
- The server serves exactly one repository name and rejects NUL bytes and
  `..` in the path before anything reaches `git-http-backend`.
- It is bound to `0.0.0.0` and is reachable by anyone holding the URL and
  token, so treat the token as a write secret and stop the server when done.
- No TLS in the server itself; the preview proxy terminates HTTPS. Do not
  expose it directly to the internet.

## Fallback

If the preview proxy does not pass Git's HTTP traffic, GitHub remains the
working channel: the agent pushes to the session branch and
`ops/pythonanywhere/sync.sh` pulls it in one command.

## Checks

`tests/test_arena_sync_server.py` pins the gating (no token = no service,
401 on missing/wrong credentials, only the one repo name reachable, `..` and
NUL rejected before `git-http-backend`) and runs a real `git clone` +
`git push` round trip against a throwaway repository on 127.0.0.1. It is part
of `python -m unittest discover -s tests`, which CI already runs.
