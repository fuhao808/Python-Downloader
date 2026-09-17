# DTCC portal downloader

`dtcc_portal.py` handles the current DTCC front door: IBM WebSEAL login plus
Akamai bot-management cookies. It opens real Chrome for authentication and
then transfers only DTCC cookies to a requests-compatible download session.
Passwords are prompted with `getpass` and are never saved by the script.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The script uses the installed Google Chrome (`channel="chrome"`), so a
separate Playwright Chromium download is not required.

## Diagnose the login page

```bash
python dtcc_portal.py probe
```

The current page should report `/pkmslogin.form`, `_abck`, `bm_sz`, and Akamai
response headers. That combination explains why an older username/password
`requests.Session()` can now fail even though the form endpoint still exists.

## Login

Manual login (best for MFA and avoids passing a username on the command line):

```bash
python dtcc_portal.py login
```

Password login with a username from an environment variable:

```bash
export DTCC_USERNAME='your-user-id'
python dtcc_portal.py login
```

Company SSO:

```bash
python dtcc_portal.py login --sso
```

Complete any MFA in the opened Chrome window. The reusable browser profile and
storage state are kept in `~/.dtcc-portal` with restrictive permissions. They
contain authenticated cookies and must be treated like credentials.

## Download

```bash
python dtcc_portal.py download \
  'https://portal.dtcc.com/path/to/file' \
  -o downloads
```

Multiple URLs are accepted. `--transport auto` (the default) tries standard
`requests` first. If Akamai rejects its TLS/browser fingerprint, it retries via
`curl-cffi` while impersonating Chrome. Both reuse the authenticated cookies
created by the real browser.

Use `--transport requests` to require the standard Requests library, or
`--headless` after a valid session has already been saved. By design, the
downloader refuses non-HTTPS and non-DTCC hosts so authenticated cookies cannot
be sent somewhere unexpected.

## Download Allocation distributions

Download every non-empty Cash and MMI distribution status for today and the
currently selected DTCC entity/participant:

```bash
python dtcc_portal.py allocations
```

Choose a settlement date and one or more entities:

```bash
python dtcc_portal.py allocations \
  --date 2026-09-15 \
  --entity ENTITY_ID_1 \
  --entity ENTITY_ID_2 \
  -o allocation_distributions
```

`--entity` can be repeated. When omitted, the entity currently selected by
DTCC is discovered automatically. `--category cash` or `--category mmi` can be
repeated to limit the export; the default downloads both. The command reads the
Allocation overview for that date and exports every non-zero status separately,
including Unallocated, Allocated, and any other status DTCC presents.

The equivalent reusable API is:

```python
from dtcc_portal import open_dtcc_session

with open_dtcc_session(transport="curl-cffi") as client:
    files = client.download_allocation_distributions(
        settlement_date=None,             # defaults to today
        entities=["ENTITY_ID_1"],         # omit to use active entity
        categories=("cash", "mmi"),
        output_dir="allocation_distributions",
    )

for item in files:
    print(item.entity, item.category, item.allocation_status, item.path)
```

## Import as a reusable session

For custom URLs and actions, import `open_dtcc_session`. The returned client
keeps the authenticated browser alive and exposes both a Requests-compatible
session and the underlying Playwright page:

```python
from dtcc_portal import open_dtcc_session

with open_dtcc_session() as client:
    # Standard requests.Session API.
    response = client.session.get(
        "https://portal.dtcc.com/your/other/url",
        timeout=60,
    )
    response.raise_for_status()
    print(response.text[:500])

    # Any custom HTTP action is also available through the convenience method.
    result = client.request(
        "POST",
        "https://portal.dtcc.com/your/api/action",
        json={"example": "value"},
    )

    # For an endpoint that needs browser JavaScript, use Playwright directly.
    client.page.goto("https://portal.dtcc.com/your/browser/page")

    # If that browser action changed cookies, copy them back before more HTTP calls.
    client.sync_cookies()
```

The simpler function below yields only the Requests-compatible session while
still keeping Chrome alive in the background:

```python
from dtcc_portal import authenticated_requests_session

with authenticated_requests_session(transport="curl-cffi") as session:
    response = session.get("https://portal.dtcc.com/your/other/url")
```

On the first call, complete login/MFA in Chrome. Later calls reuse the private
profile. Use `headless=True` only after confirming that the saved session is
still accepted.

## Limits

- A first login, new MFA challenge, CAPTCHA, or company IdP step still requires
  a real browser interaction.
- DTCC may bind a particular download endpoint to browser-only JavaScript. If
  both transports are redirected to the login/challenge page, the script stops
  instead of saving HTML as if it were the requested file.
- Never commit or share `~/.dtcc-portal`; it is an authenticated session.
