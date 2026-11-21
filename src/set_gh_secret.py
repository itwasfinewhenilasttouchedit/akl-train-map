"""Set a GitHub Actions secret on the akl-train-map repo.

Values are sealed with the repository's libsodium public key before transit,
which is how the GitHub API requires them -- the plaintext never crosses the
wire and never appears in a URL or a log line.

Reads the secret VALUE and the GitHub PAT from files passed on the command
line, so neither ever appears in argv (visible in `ps`) or in shell history.
Both files are expected to be shredded by the caller.

Usage:  set_gh_secret.py <SECRET_NAME> <value_file> <token_file>
"""
import json
import sys
import urllib.error
import urllib.request
from base64 import b64encode

from nacl import encoding, public

REPO = "itwasfinewhenilasttouchedit/akl-train-map"
API = f"https://api.github.com/repos/{REPO}"


def req(url, token, data=None, method=None):
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", f"Bearer {token}")
    r.add_header("Accept", "application/vnd.github+json")
    r.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=30) as resp:
        body = resp.read().decode() or "{}"
        return resp.status, json.loads(body) if body.strip() else {}


def main():
    name, value_file, token_file = sys.argv[1], sys.argv[2], sys.argv[3]
    value = open(value_file, "rb").read().strip().decode()
    token = open(token_file).read().strip()

    if not value:
        sys.exit(f"refusing to set {name}: value file was empty")

    # The repo's public key, used to seal the value.
    _, key = req(f"{API}/actions/secrets/public-key", token)
    sealed = public.SealedBox(
        public.PublicKey(key["key"].encode(), encoding.Base64Encoder)
    ).encrypt(value.encode())

    payload = json.dumps({
        "encrypted_value": b64encode(sealed).decode(),
        "key_id": key["key_id"],
    }).encode()

    try:
        status, _ = req(f"{API}/actions/secrets/{name}", token,
                        data=payload, method="PUT")
    except urllib.error.HTTPError as e:
        sys.exit(f"{name}: HTTP {e.code} {e.read().decode()[:200]}")

    # 201 = created, 204 = updated. Report length only, never the value.
    print(f"{name}: HTTP {status} ok ({len(value)} bytes sealed)")


if __name__ == "__main__":
    main()
