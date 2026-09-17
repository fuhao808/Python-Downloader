#!/usr/bin/env python3
"""DTCC portal login and authenticated file downloader.

The portal currently sits behind IBM WebSEAL and Akamai Bot Manager.  The
browser is therefore used to establish/refresh the authenticated session; the
resulting cookies are then handed to a requests-compatible download client.
"""

from __future__ import annotations

import argparse
import getpass
import html.parser
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse

import requests


PORTAL_URL = "https://portal.dtcc.com/"
ALLOCATION_HOME_URL = (
    "https://gcaweb.dtcc.com/gca/allocationsHome.do"
    "?newWebFlow=true&webFlow=mainWebFlow&callingApp=gca"
)
ALLOCATION_OVERVIEW_URL = "https://gcaweb.dtcc.com/allocations/getAllocationOverview.do"
DEFAULT_SESSION_DIR = Path.home() / ".dtcc-portal"
LOGIN_MARKERS = (
    b"/pkmslogin.form",
    b"DTCC Login",
    b'name="login-form-type"',
    b'placeholder="Enter Username"',
)


class DtccError(RuntimeError):
    """Base error with a user-facing message."""


class AuthenticationRequired(DtccError):
    """The response is a login/challenge page rather than the requested file."""


class _ScriptParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        src = dict(attrs).get("src")
        if src:
            self.scripts.append(src)


@dataclass(frozen=True)
class DownloadResult:
    url: str
    path: Path
    transport: str
    status_code: int


@dataclass(frozen=True)
class AllocationDistributionResult:
    entity: str
    settlement_date: date
    category: str
    allocation_status: str
    expected_count: int
    path: Path
    url: str
    transport: str


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _dtcc_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise argparse.ArgumentTypeError("download URL must be an absolute https:// URL")
    host = parsed.hostname.lower()
    if host != "dtcc.com" and not host.endswith(".dtcc.com"):
        raise argparse.ArgumentTypeError(
            f"refusing non-DTCC host {host!r}; only dtcc.com subdomains are allowed"
        )
    return value


def _looks_like_login(url: str, content_type: str, preview: bytes) -> bool:
    lower_url = url.lower()
    if "pkmslogin" in lower_url:
        return True
    if "html" not in content_type.lower() and not preview.lstrip().startswith(b"<"):
        return False
    return any(marker.lower() in preview.lower() for marker in LOGIN_MARKERS)


def _safe_filename(value: str) -> str:
    value = unquote(value).replace("\\", "/").rsplit("/", 1)[-1]
    value = re.sub(r"[\x00-\x1f\x7f]", "", value).strip().strip(".")
    return value or "download.bin"


def _response_filename(headers: Any, url: str, fallback_index: int) -> str:
    disposition = headers.get("content-disposition", "")
    utf8_match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disposition, re.I)
    plain_match = re.search(r'filename\s*=\s*(?:"([^"]+)"|([^;]+))', disposition, re.I)
    if utf8_match:
        return _safe_filename(utf8_match.group(1))
    if plain_match:
        return _safe_filename(plain_match.group(1) or plain_match.group(2))
    candidate = Path(urlparse(url).path).name
    return _safe_filename(candidate or f"download-{fallback_index}.bin")


def _unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for number in range(2, 10_000):
        alternate = directory / f"{stem}-{number}{suffix}"
        if not alternate.exists():
            return alternate
    raise DtccError(f"could not choose a unique output name for {filename!r}")


def _browser_cookie_dicts(context: Any) -> list[dict[str, Any]]:
    return [
        cookie
        for cookie in context.cookies()
        if cookie.get("domain", "").lstrip(".").endswith("dtcc.com")
    ]


def _copy_cookies(session: Any, cookies: Iterable[dict[str, Any]]) -> None:
    for cookie in cookies:
        kwargs: dict[str, Any] = {
            "domain": cookie.get("domain") or "portal.dtcc.com",
            "path": cookie.get("path") or "/",
        }
        if cookie.get("secure") is not None:
            kwargs["secure"] = bool(cookie["secure"])
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            kwargs["expires"] = int(expires)
        try:
            session.cookies.set(cookie["name"], cookie["value"], **kwargs)
        except TypeError:
            # curl-cffi's Requests-compatible Cookies.set currently accepts
            # domain/path but not Requests' secure/expires keyword arguments.
            kwargs.pop("secure", None)
            kwargs.pop("expires", None)
            session.cookies.set(cookie["name"], cookie["value"], **kwargs)


def _make_session(transport: str, cookies: list[dict[str, Any]], user_agent: str) -> Any:
    if transport == "requests":
        session: Any = requests.Session()
    elif transport == "curl-cffi":
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:
            raise DtccError(
                "curl-cffi is not installed; run: python -m pip install -r requirements.txt"
            ) from exc
        session = curl_requests.Session(impersonate="chrome")
    else:
        raise ValueError(f"unknown transport: {transport}")

    _copy_cookies(session, cookies)
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": PORTAL_URL,
        }
    )
    return session


def _download_one(
    session: Any,
    url: str,
    output_dir: Path,
    index: int,
    timeout: float,
    transport: str,
    filename_prefix: str = "",
) -> DownloadResult:
    try:
        response = session.get(url, allow_redirects=True, stream=True, timeout=timeout)
    except Exception as exc:
        raise DtccError(f"{transport} request failed for {url}: {exc}") from exc

    if response.status_code in (401, 403, 407, 429):
        response.close()
        raise AuthenticationRequired(
            f"{transport} received HTTP {response.status_code}; session or bot cookie was rejected"
        )
    try:
        response.raise_for_status()
    except Exception as exc:
        response.close()
        raise DtccError(f"HTTP error for {url}: {exc}") from exc

    iterator = response.iter_content(chunk_size=1024 * 256)
    first_chunks: list[bytes] = []
    first_size = 0
    try:
        while first_size < 128 * 1024:
            chunk = next(iterator)
            if chunk:
                first_chunks.append(chunk)
                first_size += len(chunk)
    except StopIteration:
        pass
    preview = b"".join(first_chunks)
    content_type = response.headers.get("content-type", "")
    if _looks_like_login(str(response.url), content_type, preview[: 128 * 1024]):
        response.close()
        raise AuthenticationRequired(
            f"{transport} was redirected to the DTCC login/challenge page"
        )

    filename = filename_prefix + _response_filename(response.headers, str(response.url), index)
    destination = _unique_path(output_dir, filename)
    fd, temp_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".part", dir=output_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in first_chunks:
                handle.write(chunk)
            for chunk in iterator:
                if chunk:
                    handle.write(chunk)
        os.replace(temp_name, destination)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    finally:
        response.close()

    return DownloadResult(url, destination, transport, response.status_code)


ALLOCATION_CATEGORIES = {
    "cash": {"section_id": "overviewDDV_FD", "category": "DV", "search_type": "CPY-DIST-CASH"},
    "mmi": {"section_id": "overviewDMD", "category": "MD", "search_type": "CPY-DIST-MMI"},
}


def _business_date(value: date | str | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("settlement date must use YYYY-MM-DD") from exc


def _request_for_day(day: date) -> str:
    today = date.today()
    if day < today:
        return "H"
    if day > today:
        return "F"
    return ""


def _with_query(url: str, values: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = {key: items[-1] for key, items in parse_qs(parsed.query, keep_blank_values=True).items()}
    query.update(values)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _extract_distribution_exports(
    html_text: str,
    settlement_date: date,
    entity: str,
    categories: Iterable[str],
) -> list[dict[str, Any]]:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise DtccError(
            "BeautifulSoup is required; run: python -m pip install -r requirements.txt"
        ) from exc

    soup = BeautifulSoup(html_text, "html.parser")
    expected_day = settlement_date.isoformat()
    exports: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for category_name in categories:
        definition = ALLOCATION_CATEGORIES[category_name]
        section = soup.select_one(f"#{definition['section_id']}")
        if section is None:
            continue
        for anchor in section.select('a[href*="getAllocationListFromOverview.do"]'):
            href = anchor.get("href")
            if not href:
                continue
            absolute_url = urljoin(ALLOCATION_OVERVIEW_URL, href)
            query = {
                key: values[-1]
                for key, values in parse_qs(urlparse(absolute_url).query, keep_blank_values=True).items()
            }
            if query.get("stlmtDate") != expected_day or query.get("total", "").lower() == "true":
                continue
            if query.get("category") != definition["category"]:
                continue
            if query.get("srchTypeCd") != definition["search_type"]:
                continue
            status = query.get("statusIdentifier", "unknown")
            try:
                expected_count = int(query.get("expectedCount", "0"))
            except ValueError:
                expected_count = 0
            if expected_count <= 0 or (category_name, status) in seen:
                continue
            seen.add((category_name, status))
            export_url = _with_query(
                absolute_url,
                {
                    "participantId": entity,
                    "webFlow": "appIframe",
                    "callingApp": "allocations",
                    "export": "all",
                },
            )
            exports.append(
                {
                    "entity": entity,
                    "category": category_name,
                    "status": status,
                    "expected_count": expected_count,
                    "url": export_url,
                }
            )
    return exports


def _response_is_login(response: Any) -> bool:
    content_type = response.headers.get("content-type", "")
    preview = response.content[: 128 * 1024]
    return _looks_like_login(str(response.url), content_type, preview)


def _is_login_page(page: Any) -> bool:
    try:
        return page.locator('form[action*="pkmslogin.form"]').count() > 0
    except Exception:
        return True


def _authenticated(page: Any) -> bool:
    host = (urlparse(page.url).hostname or "").lower()
    return host.endswith("dtcc.com") and not _is_login_page(page)


def _save_state(context: Any, session_dir: Path) -> None:
    target = session_dir / "storage-state.json"
    temporary = session_dir / ".storage-state.tmp.json"
    context.storage_state(path=str(temporary))
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, target)


def _complete_login(page: Any, args: argparse.Namespace) -> None:
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=args.timeout * 1000)
    if _authenticated(page):
        print("Existing DTCC browser session is still authenticated.")
        return

    print("DTCC login is required. A Chrome window is open for authentication.")
    if args.sso:
        button = page.get_by_role("button", name=re.compile("company single sign-on", re.I))
        if button.count():
            button.click()
    elif args.username:
        username = page.locator('input[name="username"]')
        password = page.locator('input[name="password"]')
        if not username.count() or not password.count():
            raise DtccError("the expected DTCC username/password fields were not found")
        secret = getpass.getpass("DTCC password (not stored): ")
        username.fill(args.username)
        password.fill(secret)
        page.get_by_role("button", name=re.compile(r"^login$", re.I)).click()
        del secret
    else:
        print("Complete username/password, SSO, and any MFA steps in the browser.")

    deadline = time.monotonic() + args.login_timeout
    while time.monotonic() < deadline:
        if _authenticated(page):
            print(f"Authenticated DTCC page reached: {page.url}")
            return
        page.wait_for_timeout(1000)
    raise AuthenticationRequired(
        f"login did not complete within {args.login_timeout:.0f} seconds; rerun and finish the browser flow"
    )


class DtccPortalSession:
    """Authenticated DTCC browser plus a requests-compatible HTTP session.

    Use this class as a context manager.  ``page`` is the live Playwright page
    for browser-only actions, while ``session`` is either requests.Session or
    curl-cffi's compatible Session for ordinary HTTP calls.
    """

    def __init__(
        self,
        *,
        session_dir: str | Path = DEFAULT_SESSION_DIR,
        username: str | None = None,
        sso: bool = False,
        headless: bool = False,
        login_timeout: float = 300,
        timeout: float = 60,
        transport: str = "requests",
    ) -> None:
        if transport not in {"requests", "curl-cffi"}:
            raise ValueError("transport must be 'requests' or 'curl-cffi'")
        self.session_dir = Path(session_dir).expanduser().resolve()
        self.username = username if username is not None else os.environ.get("DTCC_USERNAME")
        self.sso = sso
        self.headless = headless
        self.login_timeout = login_timeout
        self.timeout = timeout
        self.transport = transport

        self.playwright: Any | None = None
        self.context: Any | None = None
        self.page: Any | None = None
        self.session: Any | None = None

    def __enter__(self) -> "DtccPortalSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise DtccError(
                "Playwright is required; run: python -m pip install -r requirements.txt"
            ) from exc

        _private_dir(self.session_dir)
        profile_dir = _private_dir(self.session_dir / "chrome-profile")
        try:
            self.playwright = sync_playwright().start()
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=self.headless,
                accept_downloads=True,
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            login_args = argparse.Namespace(
                timeout=self.timeout,
                login_timeout=self.login_timeout,
                username=self.username,
                sso=self.sso,
            )
            _complete_login(self.page, login_args)
            self.new_http_session(self.transport)
            return self
        except BaseException:
            self.close(save_state=False)
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()

    def new_http_session(self, transport: str | None = None) -> Any:
        """Create a fresh HTTP session from the current browser cookies."""
        if self.context is None or self.page is None:
            raise DtccError("DTCC client is not open; use it inside a 'with' block")
        selected = transport or self.transport
        if selected not in {"requests", "curl-cffi"}:
            raise ValueError("transport must be 'requests' or 'curl-cffi'")
        old_session = self.session
        user_agent = self.page.evaluate("navigator.userAgent")
        self.session = _make_session(
            selected,
            _browser_cookie_dicts(self.context),
            user_agent,
        )
        self.transport = selected
        if old_session is not None:
            old_session.close()
        return self.session

    def sync_cookies(self) -> Any:
        """Copy cookies changed by browser actions into the existing HTTP session."""
        if self.context is None or self.session is None:
            raise DtccError("DTCC client is not open; use it inside a 'with' block")
        _copy_cookies(self.session, _browser_cookie_dicts(self.context))
        return self.session

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Send an HTTP request through the authenticated requests-compatible session."""
        if self.session is None:
            raise DtccError("DTCC client is not open; use it inside a 'with' block")
        kwargs.setdefault("timeout", self.timeout)
        return self.session.request(method, url, **kwargs)

    def download_allocation_distributions(
        self,
        *,
        settlement_date: date | str | None = None,
        entities: Iterable[str] | None = None,
        categories: Iterable[str] = ("cash", "mmi"),
        output_dir: str | Path = "allocation_distributions",
    ) -> list[AllocationDistributionResult]:
        """Download every non-empty Cash/MMI distribution export for a date."""
        return download_allocation_distributions(
            self,
            settlement_date=settlement_date,
            entities=entities,
            categories=categories,
            output_dir=output_dir,
        )

    def close(self, *, save_state: bool = True) -> None:
        """Persist browser state and release HTTP/browser resources."""
        if self.session is not None:
            self.session.close()
            self.session = None
        if self.context is not None:
            try:
                if save_state:
                    _save_state(self.context, self.session_dir)
            finally:
                self.context.close()
                self.context = None
                self.page = None
        if self.playwright is not None:
            self.playwright.stop()
            self.playwright = None


def open_dtcc_session(
    *,
    session_dir: str | Path = DEFAULT_SESSION_DIR,
    username: str | None = None,
    sso: bool = False,
    headless: bool = False,
    login_timeout: float = 300,
    timeout: float = 60,
    transport: str = "requests",
) -> DtccPortalSession:
    """Return a context manager providing an authenticated browser and HTTP session."""
    return DtccPortalSession(
        session_dir=session_dir,
        username=username,
        sso=sso,
        headless=headless,
        login_timeout=login_timeout,
        timeout=timeout,
        transport=transport,
    )


def download_allocation_distributions(
    client: DtccPortalSession,
    *,
    settlement_date: date | str | None = None,
    entities: Iterable[str] | None = None,
    categories: Iterable[str] = ("cash", "mmi"),
    output_dir: str | Path = "allocation_distributions",
) -> list[AllocationDistributionResult]:
    """Download all visible Cash/MMI distribution statuses for a settlement date.

    ``settlement_date`` defaults to the local current date.  When ``entities``
    is omitted, the participant/entity currently selected by DTCC is used.
    """
    if client.context is None or client.page is None or client.session is None:
        raise DtccError("DTCC client is not open; use it inside a 'with' block")

    day = _business_date(settlement_date)
    selected_categories = tuple(dict.fromkeys(value.lower() for value in categories))
    invalid_categories = set(selected_categories).difference(ALLOCATION_CATEGORIES)
    if invalid_categories:
        raise ValueError(f"unsupported allocation categories: {', '.join(sorted(invalid_categories))}")
    if not selected_categories:
        raise ValueError("at least one allocation category is required")

    client.page.goto(
        ALLOCATION_HOME_URL,
        wait_until="domcontentloaded",
        timeout=client.timeout * 1000,
    )
    if _is_login_page(client.page):
        raise AuthenticationRequired("DTCC session expired while opening Corporate Actions Allocations")
    try:
        participant = client.page.locator(".dtcc-header-partid").first
        participant.wait_for(state="visible", timeout=client.timeout * 1000)
        active_entity = (participant.text_content() or "").strip()
    except Exception as exc:
        raise DtccError(
            "Corporate Actions Allocations did not load or the active entity could not be found"
        ) from exc
    client.sync_cookies()

    if entities is None:
        entity_ids = [active_entity]
    else:
        entity_ids = list(dict.fromkeys(str(value).strip() for value in entities if str(value).strip()))
    if not entity_ids:
        raise ValueError("no allocation entity/participant IDs were provided or discovered")
    for entity in entity_ids:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", entity):
            raise ValueError(f"invalid allocation entity/participant ID: {entity!r}")

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    results: list[AllocationDistributionResult] = []

    for entity in entity_ids:
        params = {
            "requestFor": _request_for_day(day),
            "participantId": entity,
            "eventGroupCode": "D",
            "webFlow": "appIframe",
            "newWebFlow": "true",
            "callingApp": "allocations",
        }
        if day > date.today():
            params["statusIdentifier"] = "H"
        overview = client.request("GET", ALLOCATION_OVERVIEW_URL, params=params)
        try:
            overview.raise_for_status()
            if _response_is_login(overview):
                raise AuthenticationRequired(
                    f"DTCC returned a login/challenge page for allocation entity {entity}"
                )
            exports = _extract_distribution_exports(
                overview.text,
                day,
                entity,
                selected_categories,
            )
        finally:
            overview.close()

        for index, export in enumerate(exports, start=1):
            safe_prefix = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "-",
                f"{entity}_{day.isoformat()}_{export['category']}_{export['status']}_",
            )
            downloaded = _download_one(
                client.session,
                export["url"],
                destination,
                index,
                client.timeout,
                client.transport,
                filename_prefix=safe_prefix,
            )
            results.append(
                AllocationDistributionResult(
                    entity=entity,
                    settlement_date=day,
                    category=export["category"],
                    allocation_status=export["status"],
                    expected_count=export["expected_count"],
                    path=downloaded.path,
                    url=export["url"],
                    transport=downloaded.transport,
                )
            )
    return results


@contextmanager
def authenticated_requests_session(**kwargs: Any) -> Iterator[Any]:
    """Yield only the authenticated requests-compatible session.

    Prefer ``open_dtcc_session`` when browser actions or cookie resync may be
    needed.  The browser stays alive until this context manager exits.
    """
    with open_dtcc_session(**kwargs) as client:
        yield client.session


def _with_browser(args: argparse.Namespace, action: Any) -> Any:
    with open_dtcc_session(
        session_dir=args.session_dir,
        username=args.username,
        sso=args.sso,
        headless=args.headless,
        login_timeout=args.login_timeout,
        timeout=args.timeout,
    ) as client:
        return action(client.context, client.page, client.session_dir)


def command_probe(args: argparse.Namespace) -> int:
    session = requests.Session()
    response = session.get(PORTAL_URL, timeout=args.timeout)
    parser = _ScriptParser()
    parser.feed(response.text)
    print(f"status: {response.status_code}")
    print(f"final URL: {response.url}")
    print(f"login endpoint present: {'/pkmslogin.form' in response.text}")
    print(f"cookies set: {', '.join(sorted(session.cookies.get_dict())) or '(none)'}")
    print(f"x-akamai-transformed: {response.headers.get('x-akamai-transformed', '(absent)')}")
    print(f"akamai-grn: {response.headers.get('akamai-grn', '(absent)')}")
    print("script assets:")
    for script in parser.scripts:
        print(f"  {script}")
    akamai = {"_abck", "bm_sz"}.intersection(session.cookies.get_dict())
    if akamai:
        print("diagnosis: Akamai bot-management cookies/scripts are active; use browser login before requests.")
    return 0


def command_login(args: argparse.Namespace) -> int:
    def login_action(context: Any, page: Any, session_dir: Path) -> None:
        del context, page, session_dir

    _with_browser(args, login_action)
    print(f"Session saved under {Path(args.session_dir).expanduser()}")
    return 0


def command_download(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    def download_action(context: Any, page: Any, session_dir: Path) -> list[DownloadResult]:
        del session_dir
        cookies = _browser_cookie_dicts(context)
        user_agent = page.evaluate("navigator.userAgent")
        transports = [args.transport] if args.transport != "auto" else ["requests", "curl-cffi"]
        results: list[DownloadResult] = []
        for index, url in enumerate(args.urls, start=1):
            failures: list[str] = []
            for transport in transports:
                try:
                    session = _make_session(transport, cookies, user_agent)
                    result = _download_one(
                        session, url, output_dir, index, args.timeout, transport
                    )
                    results.append(result)
                    print(f"Downloaded with {transport}: {result.path}")
                    break
                except (AuthenticationRequired, DtccError) as exc:
                    failures.append(str(exc))
            else:
                joined = "; ".join(failures)
                raise AuthenticationRequired(
                    f"all transports failed for {url}: {joined}. "
                    "The endpoint may require browser-only navigation or the session may have expired."
                )
        return results

    _with_browser(args, download_action)
    return 0


def command_allocations(args: argparse.Namespace) -> int:
    with open_dtcc_session(
        session_dir=args.session_dir,
        username=args.username,
        sso=args.sso,
        headless=args.headless,
        login_timeout=args.login_timeout,
        timeout=args.timeout,
        transport=args.transport,
    ) as client:
        results = client.download_allocation_distributions(
            settlement_date=args.settlement_date,
            entities=args.entities,
            categories=args.categories or ("cash", "mmi"),
            output_dir=args.output_dir,
        )
    if not results:
        day = _business_date(args.settlement_date)
        print(f"No non-empty Cash/MMI distribution exports were found for {day.isoformat()}.")
        return 0
    for result in results:
        print(
            f"Downloaded entity={result.entity} category={result.category} "
            f"status={result.allocation_status} count={result.expected_count}: {result.path}"
        )
    return 0


def _browser_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--session-dir",
        default=str(DEFAULT_SESSION_DIR),
        help=f"private browser/session directory (default: {DEFAULT_SESSION_DIR})",
    )
    parser.add_argument("--username", default=os.environ.get("DTCC_USERNAME"))
    parser.add_argument("--sso", action="store_true", help="start company SSO instead of password login")
    parser.add_argument("--headless", action="store_true", help="only works while no interactive login/MFA is needed")
    parser.add_argument("--login-timeout", type=float, default=300, help="seconds allowed for login/MFA")
    parser.add_argument("--timeout", type=float, default=60, help="network/navigation timeout in seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", help="inspect the public login front door")
    probe.add_argument("--timeout", type=float, default=30)
    probe.set_defaults(func=command_probe)

    login = subparsers.add_parser("login", help="authenticate in Chrome and persist the session")
    _browser_options(login)
    login.set_defaults(func=command_login)

    download = subparsers.add_parser("download", help="login/refresh, then download one or more URLs")
    _browser_options(download)
    download.add_argument("urls", nargs="+", type=_dtcc_url)
    download.add_argument("-o", "--output-dir", default="downloads")
    download.add_argument(
        "--transport",
        choices=("auto", "requests", "curl-cffi"),
        default="auto",
        help="auto tries requests first, then Chrome-impersonating curl-cffi",
    )
    download.set_defaults(func=command_download)

    allocations = subparsers.add_parser(
        "allocations",
        help="download all Cash/MMI distribution exports for a settlement date",
    )
    _browser_options(allocations)
    allocations.add_argument(
        "--date",
        dest="settlement_date",
        type=_business_date,
        help="settlement date in YYYY-MM-DD format (default: today)",
    )
    allocations.add_argument(
        "--entity",
        dest="entities",
        action="append",
        help="entity/participant ID; repeat for multiple entities (default: active entity)",
    )
    allocations.add_argument(
        "--category",
        dest="categories",
        action="append",
        choices=tuple(ALLOCATION_CATEGORIES),
        help="category to export; repeat as needed (default: cash and mmi)",
    )
    allocations.add_argument("-o", "--output-dir", default="allocation_distributions")
    allocations.add_argument(
        "--transport",
        choices=("requests", "curl-cffi"),
        default="curl-cffi",
        help="authenticated HTTP transport (default: curl-cffi)",
    )
    allocations.set_defaults(func=command_allocations)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except DtccError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
