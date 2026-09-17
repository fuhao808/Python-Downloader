"""Minimal DTCC login + parameterized report downloader.

Install once:
    py -m pip install requests playwright

This uses the installed Google Chrome, so `playwright install` is normally not
required.  Chrome stays open until all downloads finish so DTCC/Akamai session
cookies remain valid.
"""

import datetime
import getpass
import os
import re
import tempfile
from pathlib import Path

import requests
import urllib3
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


PORTAL_URL = "https://portal.dtcc.com/"
ALLOCATION_HOME_URL = (
    "https://gcaweb.dtcc.com/gca/allocationsHome.do"
    "?newWebFlow=true&webFlow=mainWebFlow&callingApp=gca"
)


def login(username, password, proxy=None, login_timeout=180):
    """Log in through real Chrome and return (session, context, playwright).

    Keep `context` and `playwright` open until downloads have finished.  The
    returned `session` is a normal requests.Session.
    """
    playwright = sync_playwright().start()
    profile_dir = os.path.join(tempfile.gettempdir(), "dtcc_downloader_chrome_profile")
    context = None

    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            channel="chrome",
            headless=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)

        username_field = page.locator('input[name="username"]')
        password_field = page.locator('input[name="password"]')

        # A saved session may already be logged in.
        if username_field.count() and username_field.first.is_visible():
            username_field.first.fill(username)
            password_field.first.fill(password)
            page.locator('button[type="submit"]').first.click()

        print("Complete MFA in Chrome if DTCC requests it.")

        # Opening the application proves that authentication has completed and
        # also creates the gcaweb.dtcc.com application cookies.
        try:
            page.goto(
                ALLOCATION_HOME_URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.locator("iframe#frmappIframe").wait_for(
                state="attached",
                timeout=15_000,
            )
        except PlaywrightTimeoutError:
            input("Finish MFA/login in Chrome, then press Enter here: ")
            page.goto(
                ALLOCATION_HOME_URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.locator("iframe#frmappIframe").wait_for(
                state="attached",
                timeout=login_timeout * 1_000,
            )

        # Fail clearly instead of returning a session that only contains the
        # DTCC login page.
        if page.locator('input[name="username"]').count():
            if page.locator('input[name="username"]').first.is_visible():
                raise RuntimeError("DTCC login failed or did not complete")

        session = requests.Session()
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})

        user_agent = page.evaluate("navigator.userAgent")
        session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": ALLOCATION_HOME_URL,
            }
        )

        for cookie in context.cookies():
            domain = cookie.get("domain", "")
            if domain.lstrip(".").endswith("dtcc.com"):
                session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=domain,
                    path=cookie.get("path", "/"),
                )

        print("Login successful")
        return session, context, playwright

    except BaseException:
        try:
            context.close()
        except Exception:
            pass
        playwright.stop()
        raise


def download_reports(
    session,
    base_url,
    reports,
    common_params=None,
    run_date=None,
    output_dir="downloads",
    verify_tls=False,
    timeout=120,
):
    """Download reports using one base URL plus per-report parameters.

    `reports` is a list of dictionaries with:
      - filename: output filename; YYYY-MM-DD is replaced automatically
      - params: parameters specific to this report
      - base_url: optional override for this report

    Returns a list of downloaded pathlib.Path objects.
    """
    run_date = run_date or datetime.date.today().strftime("%Y-%m-%d")
    if isinstance(run_date, (datetime.date, datetime.datetime)):
        run_date = run_date.strftime("%Y-%m-%d")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    common_params = dict(common_params or {})
    downloaded = []

    if not verify_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    for report in reports:
        report_url = report.get("base_url", base_url)
        params = {**common_params, **report.get("params", {})}
        params = {
            key: value.replace("YYYY-MM-DD", run_date)
            if isinstance(value, str)
            else value
            for key, value in params.items()
        }

        filename = report["filename"].replace("YYYY-MM-DD", run_date)
        filename = re.sub(r"[<>:\"/\\|?*]", "_", filename)
        output_path = output_dir / filename

        response = session.get(
            report_url,
            params=params,
            timeout=timeout,
            verify=verify_tls,
            allow_redirects=True,
            stream=True,
        )
        try:
            response.raise_for_status()

            chunks = response.iter_content(chunk_size=128 * 1024)
            preview = next(chunks, b"")
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type and (
                b"pkmslogin.form" in preview.lower()
                or b"enter username" in preview.lower()
                or b"dtcc login" in preview.lower()
            ):
                raise RuntimeError(
                    f"Session expired or DTCC rejected the request: {response.url}"
                )

            with output_path.open("wb") as file_handle:
                if preview:
                    file_handle.write(preview)
                for chunk in chunks:
                    if chunk:
                        file_handle.write(chunk)

            downloaded.append(output_path)
            print(f"Downloaded: {output_path}")
        finally:
            response.close()

    return downloaded


if __name__ == "__main__":
    # Optional proxy. Set HTTPS_PROXY in your environment when required.
    PROXY = os.environ.get("HTTPS_PROXY")

    # This is the endpoint seen in DTCC Allocation export links.
    BASE_URL = (
        "https://gcaweb.dtcc.com/allocations/"
        "getAllocationListFromOverview.do"
    )

    # Parameters shared by every Allocation Distribution export.
    COMMON_PARAMS = {
        "requestFor": "H",
        "stlmtDate": "YYYY-MM-DD",
        "eventType": "",
        "eventTypeLabel": "",
        "eventGroupCode": "D",
        "export": "all",
        "webFlow": "appIframe",
        "callingApp": "allocations",
    }

    # Add/remove reports here. Each report only contains its extra parameters.
    REPORTS = [
        {
            "filename": "YYYY-MM-DD-ENTITY1-ALLOC-CSH.csv",
            "params": {
                "srchTypeCd": "CPY-DIST-CASH",
                "category": "DV",
                "participantId": "YOUR_ENTITY_ID",
                "statusIdentifier": "UA",
            },
        },
        {
            "filename": "YYYY-MM-DD-ENTITY1-ALLOC-MMI.csv",
            "params": {
                "srchTypeCd": "CPY-DIST-MMI",
                "category": "MD",
                "participantId": "YOUR_ENTITY_ID",
                "statusIdentifier": "UA",
            },
        },
        # Example for another entity:
        # {
        #     "filename": "YYYY-MM-DD-ENTITY2-ALLOC-CSH.csv",
        #     "params": {
        #         "srchTypeCd": "CPY-DIST-CASH",
        #         "category": "DV",
        #         "participantId": "YOUR_OTHER_ENTITY",
        #         "statusIdentifier": "UA",
        #     },
        # },
    ]

    # Replace this with the network folder from your original script.
    OUTPUT_DIR = "downloads"

    username = input("DTCC username: ").strip()
    password = getpass.getpass("DTCC password: ")

    session = context = playwright = None
    try:
        session, context, playwright = login(
            username=username,
            password=password,
            proxy=PROXY,
        )
        download_reports(
            session=session,
            base_url=BASE_URL,
            reports=REPORTS,
            common_params=COMMON_PARAMS,
            run_date=None,  # None means today
            output_dir=OUTPUT_DIR,
            verify_tls=False,
        )
    finally:
        if session is not None:
            session.close()
        if context is not None:
            context.close()
        if playwright is not None:
            playwright.stop()
