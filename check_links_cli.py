"""
Video Link Checker - CLI (GitHub Actions icin)
Kullanim:
    # Tek JSON
    python check_links_cli.py --json links.json

    # Manifest (icindeki file URL'lerini cekip hepsini kontrol eder)
    python check_links_cli.py --manifest https://cdn.jsdelivr.net/gh/user/repo@main/series_manifest.json

Ortam degiskenleri (opsiyonel - e-posta icin):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
    MAIL_TO, MAIL_FROM (MAIL_FROM bos ise SMTP_USER kullanilir)
    MAIL_SUBJECT_PREFIX (opsiyonel)
"""

import argparse
import asyncio
import csv
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import aiohttp

# ----------------------------- Ayarlar -------------------------------------
CONCURRENT_WORKERS = 30
REQUEST_TIMEOUT    = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

OK_STATUS     = "\u2713 \u00c7al\u0131\u015f\u0131yor"
BROKEN_STATUS = "\u2717 Bozuk"
REDIR_STATUS  = "\u26a0 Y\u00f6nlendi"

# URL -> baslik, URL -> kaynak dosya adi
URL_META: dict   = {}
URL_SOURCE: dict = {}

# ----------------------------- Yukleme -------------------------------------
def _read_url(url):
    """Cache-bypass ile URL indirir."""
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    with urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8")

def load_json(path_or_url):
    if path_or_url.startswith(("http://", "https://")):
        return json.loads(_read_url(path_or_url))
    with open(path_or_url, encoding="utf-8") as f:
        return json.load(f)

def bust_cache(url):
    """jsDelivr gibi CDN'leri baypas etmek icin zaman damgasi ekler."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_t={int(time.time())}"

# ----------------------------- URL cikarma ---------------------------------
def extract_urls(obj, found=None, skip_keys=None, _title=""):
    if found is None:
        found = []
    if skip_keys is None:
        skip_keys = {"cover", "thumbnail"}

    if isinstance(obj, dict):
        current_title = obj.get("title") or _title
        for k, v in obj.items():
            if k.lower() in skip_keys:
                continue
            extract_urls(v, found, skip_keys, current_title)
    elif isinstance(obj, list):
        for item in obj:
            extract_urls(item, found, skip_keys, _title)
    elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
        if obj not in found:
            found.append(obj)
        URL_META[obj] = _title
    return found

def get_domain(url):
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""

# ----------------------------- Tespit mantigi ------------------------------
ERROR_PATTERNS_STR = [
    "video not found", "video does not exist", "video deleted",
    "video removed", "video unavailable", "page not found",
    "404 not found", "file not found", "content not found",
    "no longer available", "has been deleted", "has been removed",
    "doesn't exist", "does not exist",
    "\u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d",
    "\u0443\u0434\u0430\u043b\u0435\u043d",
    "\u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f",
    "\u0432\u0438\u0434\u0435\u043e \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e",
    "video bulunamad",
]

def has_error_pattern(t):
    return any(p in t for p in ERROR_PATTERNS_STR)

_VIDEO_SRC_RE = re.compile(
    r'(?:'
    r'''['"]?file['"]?\s*:\s*['"](https?:)?//[^'"<>\s]{8,}'''
    r'|'
    r'''"url"\s*:\s*"https?://[^"<>\s]{8,}'''
    r'|'
    r'''src\s*=\s*['"]https?://[^'"<>\s]{8,}\.(?:mp4|flv|m3u8|webm|ogg)'''
    r'|'
    r'https?://[^\s"\'<>]{8,}\.(?:mp4|flv|m3u8|webm)(?:[?#][^\s"\'<>]*)?'
    r')',
    re.IGNORECASE,
)

def has_video_src(t):
    return bool(_VIDEO_SRC_RE.search(t))

PLAYER_HINTS = ["player", "playlist", "mp4", "m3u8", "flv", "videoid", "videofile", "jwplayer"]
def looks_like_player(t):
    return sum(1 for h in PLAYER_HINTS if h in t) >= 3

HTML_PLAYER_DOMAINS = [
    "sibnet.ru", "rutube.ru", "ok.ru", "vk.com",
    "mail.ru", "myvi.ru", "videoapi.my.mail.ru",
]
def is_html_player_site(url):
    d = get_domain(url)
    return any(p in d for p in HTML_PLAYER_DOMAINS)

STREAM_MANIFEST_EXTS = (".m3u8", ".mpd", ".ism", ".isml")
def is_streaming_manifest(url):
    try:
        return urlparse(url).path.lower().endswith(STREAM_MANIFEST_EXTS)
    except Exception:
        return False

# ----------------------------- Kontroller ----------------------------------
async def check_stream_manifest(session, url):
    try:
        async with session.get(
            url, headers={"User-Agent": USER_AGENT, "Referer": url},
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), ssl=False,
        ) as resp:
            code = resp.status
            final_url = str(resp.url)
            redirected = final_url.rstrip("/") != url.rstrip("/")
            if code >= 400:
                return BROKEN_STATUS, code, ""
            raw = await resp.content.read(131072)
            t = raw.decode("utf-8", errors="replace").lower()
            if "#extm3u" in t or "#ext-x-" in t or "<mpd" in t or "<smoothstreamingmedia" in t:
                return (REDIR_STATUS if redirected else OK_STATUS,
                        code, final_url if redirected else "")
            if has_error_pattern(t):
                return BROKEN_STATUS, "Icerik Hatasi", ""
            return BROKEN_STATUS, "Gecersiz Manifest", ""
    except asyncio.TimeoutError:
        return BROKEN_STATUS, "Zaman Asimi", ""
    except aiohttp.ClientConnectorError:
        return BROKEN_STATUS, "Baglanti Hatasi", ""
    except Exception as e:
        return BROKEN_STATUS, str(e)[:40], ""

async def check_html_content(session, url):
    try:
        async with session.get(
            url, headers={"User-Agent": USER_AGENT, "Referer": url},
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), ssl=False,
        ) as resp:
            code = resp.status
            final_url = str(resp.url)
            redirected = final_url.rstrip("/") != url.rstrip("/")
            if code >= 400:
                return BROKEN_STATUS, code, ""
            raw = await resp.content.read(131072)
            t = raw.decode("utf-8", errors="replace")
            tl = t.lower()
            if has_error_pattern(tl):
                return BROKEN_STATUS, "Icerik Hatasi", ""
            if looks_like_player(tl) and not has_video_src(t):
                return BROKEN_STATUS, "Video Yok", ""
            return (REDIR_STATUS if redirected else OK_STATUS,
                    code, final_url if redirected else "")
    except asyncio.TimeoutError:
        return BROKEN_STATUS, "Zaman Asimi", ""
    except aiohttp.ClientConnectorError:
        return BROKEN_STATUS, "Baglanti Hatasi", ""
    except Exception as e:
        return BROKEN_STATUS, str(e)[:40], ""

async def check_url(session, url, semaphore):
    async with semaphore:
        if is_streaming_manifest(url):
            return (url, *await check_stream_manifest(session, url))
        if is_html_player_site(url):
            return (url, *await check_html_content(session, url))

        head_failed = False
        try:
            async with session.head(
                url, headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), ssl=False,
            ) as resp:
                code = resp.status
                final_url = str(resp.url)
                ctype = resp.headers.get("Content-Type", "")
                redirected = final_url.rstrip("/") != url.rstrip("/")
                if code not in (400, 401, 403, 405, 406, 501, 502, 503):
                    if code >= 400:
                        return url, BROKEN_STATUS, code, ""
                    if "text/html" not in ctype:
                        return (url, REDIR_STATUS if redirected else OK_STATUS,
                                code, final_url if redirected else "")
                else:
                    head_failed = True
        except Exception:
            head_failed = True

        if head_failed:
            try:
                async with session.get(
                    url,
                    headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0", "Referer": url},
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), ssl=False,
                ) as resp:
                    await resp.content.readany()
                    code = resp.status
                    final_url = str(resp.url)
                    ctype = resp.headers.get("Content-Type", "")
                    redirected = final_url.rstrip("/") != url.rstrip("/")
                    if code >= 400:
                        return url, BROKEN_STATUS, code, ""
                    if "text/html" not in ctype:
                        return (url, REDIR_STATUS if redirected else OK_STATUS,
                                code, final_url if redirected else "")
            except aiohttp.ClientConnectorError:
                return url, BROKEN_STATUS, "Baglanti Hatasi", ""
            except asyncio.TimeoutError:
                return url, BROKEN_STATUS, "Zaman Asimi", ""
            except Exception as e:
                return url, BROKEN_STATUS, str(e)[:40], ""

        return (url, *await check_html_content(session, url))

async def run_all(urls, on_progress=None):
    sem = asyncio.Semaphore(CONCURRENT_WORKERS)
    connector = aiohttp.TCPConnector(limit=CONCURRENT_WORKERS, ssl=False)
    results = {}
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [check_url(session, u, sem) for u in urls]
        done = 0
        for coro in asyncio.as_completed(tasks):
            u, st, code, redirect = await coro
            results[u] = (st, code, redirect)
            done += 1
            if on_progress:
                on_progress(done, len(urls))
    return results

# ----------------------------- Manifest isleme -----------------------------
async def fetch_manifest_sources(manifest_url):
    """
    Manifest'i indirir, icindeki files[] URL'lerini paralel ceker,
    her dosyadan URL'leri cikarir. (all_urls, url_source) dondurur.
    """
    print(f"[+] Manifest yukleniyor: {manifest_url}")
    manifest = load_json(bust_cache(manifest_url))

    if isinstance(manifest, dict):
        source_urls = manifest.get("files") or []
    elif isinstance(manifest, list):
        source_urls = manifest
    else:
        source_urls = []

    source_urls = [u for u in source_urls
                   if isinstance(u, str) and u.startswith(("http://", "https://"))]
    print(f"[+] Manifest icinde {len(source_urls)} kaynak dosya bulundu")

    all_urls = []
    seen = set()

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=8, ssl=False)
    ) as session:
        for i, src in enumerate(source_urls, 1):
            src_name = Path(urlparse(src).path).name or f"file{i}"
            print(f"    [{i}/{len(source_urls)}] {src_name}")
            try:
                # CDN cache'i atlamak icin hem header hem query param
                fetch_url = bust_cache(src)
                async with session.get(
                    fetch_url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    },
                    timeout=aiohttp.ClientTimeout(total=60), ssl=False,
                ) as r:
                    r.raise_for_status()
                    data = await r.json(content_type=None)
            except Exception as e:
                print(f"        [!] {src_name} yuklenemedi: {str(e)[:80]}")
                continue

            src_urls = extract_urls(data, _title="")
            new = 0
            for u in src_urls:
                if u not in seen:
                    seen.add(u)
                    URL_SOURCE[u] = src_name
                    all_urls.append(u)
                    new += 1
            print(f"        {new} yeni URL ({len(src_urls)} toplam)")

    return all_urls, URL_SOURCE

# ----------------------------- Rapor ---------------------------------------
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def _source_summary_rows(urls, results):
    """Kaynak bazli ozet satirlari uretir."""
    agg = {}
    for u in urls:
        src = URL_SOURCE.get(u, "—")
        st = results.get(u, ("-",))[0]
        a = agg.setdefault(src, {"total": 0, "ok": 0, "brk": 0, "red": 0})
        a["total"] += 1
        if st == OK_STATUS:    a["ok"] += 1
        elif st == BROKEN_STATUS: a["brk"] += 1
        elif st == REDIR_STATUS:  a["red"] += 1

    rows = []
    for src, a in sorted(agg.items()):
        brk_color = "#ef4444" if a["brk"] else "#94a3b8"
        rows.append(f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #e2e8f0;font-family:monospace">{_esc(src)}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:right">{a['total']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:right;color:#22c55e">{a['ok']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:right;color:{brk_color};font-weight:600">{a['brk']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:right;color:#f59e0b">{a['red']}</td>
        </tr>""")
    return "".join(rows)

def build_html_report(results, urls, elapsed):
    ok  = sum(1 for u in urls if results.get(u, ("",))[0] == OK_STATUS)
    brk = sum(1 for u in urls if results.get(u, ("",))[0] == BROKEN_STATUS)
    red = sum(1 for u in urls if results.get(u, ("",))[0] == REDIR_STATUS)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    color = {OK_STATUS: "#22c55e", BROKEN_STATUS: "#ef4444",
             REDIR_STATUS: "#f59e0b"}

    rows = []
    for i, u in enumerate(urls, 1):
        st, code, redirect = results.get(u, ("-", "-", ""))
        c = color.get(st, "#94a3b8")
        title  = _esc(URL_META.get(u, "") or "—")
        source = _esc(URL_SOURCE.get(u, "—"))
        rows.append(f"""
        <tr>
          <td style="text-align:center;color:#64748b">{i}</td>
          <td style="color:#64748b;font-family:monospace;font-size:11px">{source}</td>
          <td>{title}</td>
          <td style="color:{c};font-weight:600;white-space:nowrap">{st}</td>
          <td style="text-align:center;font-family:monospace">{code}</td>
          <td><a href="{_esc(u)}" style="color:#4f8cff;text-decoration:none;word-break:break-all">{_esc(u)}</a></td>
          <td style="color:#64748b;word-break:break-all">{_esc(redirect)}</td>
        </tr>""")

    summary_rows = _source_summary_rows(urls, results)

    return f"""<!DOCTYPE html>
<html lang="tr"><head><meta charset="utf-8">
<title>Video Link Raporu</title></head>
<body style="margin:0;background:#f1f5f9;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#0f172a">
<div style="max-width:1300px;margin:0 auto;padding:24px">

  <h1 style="margin:0 0 4px">🎬 Video Link Raporu</h1>
  <p style="margin:0 0 24px;color:#64748b">{now} &middot; {len(urls)} link &middot; {elapsed:.1f} sn</p>

  <table style="width:100%;border-collapse:separate;border-spacing:12px 0;margin-bottom:24px">
    <tr>
      <td style="background:#fff;border-radius:10px;padding:16px 20px;border-left:4px solid #4f8cff">
        <div style="color:#64748b;font-size:11px;font-weight:600;letter-spacing:.5px">TOPLAM</div>
        <div style="font-size:28px;font-weight:700">{len(urls)}</div>
      </td>
      <td style="background:#fff;border-radius:10px;padding:16px 20px;border-left:4px solid #22c55e">
        <div style="color:#64748b;font-size:11px;font-weight:600;letter-spacing:.5px">ÇALIŞIYOR</div>
        <div style="font-size:28px;font-weight:700;color:#22c55e">{ok}</div>
      </td>
      <td style="background:#fff;border-radius:10px;padding:16px 20px;border-left:4px solid #ef4444">
        <div style="color:#64748b;font-size:11px;font-weight:600;letter-spacing:.5px">BOZUK</div>
        <div style="font-size:28px;font-weight:700;color:#ef4444">{brk}</div>
      </td>
      <td style="background:#fff;border-radius:10px;padding:16px 20px;border-left:4px solid #f59e0b">
        <div style="color:#64748b;font-size:11px;font-weight:600;letter-spacing:.5px">YÖNLENDİ</div>
        <div style="font-size:28px;font-weight:700;color:#f59e0b">{red}</div>
      </td>
    </tr>
  </table>

  <h2 style="font-size:16px;margin:0 0 12px">Kaynak Bazlı Özet</h2>
  <div style="background:#fff;border-radius:10px;overflow:hidden;margin-bottom:24px">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead>
        <tr style="background:#0f172a;color:#e2e8f0">
          <th style="padding:10px 12px;text-align:left">Kaynak</th>
          <th style="padding:10px 12px;text-align:right">Toplam</th>
          <th style="padding:10px 12px;text-align:right">OK</th>
          <th style="padding:10px 12px;text-align:right">Bozuk</th>
          <th style="padding:10px 12px;text-align:right">Yönlendi</th>
        </tr>
      </thead>
      <tbody>{summary_rows}</tbody>
    </table>
  </div>

  <h2 style="font-size:16px;margin:0 0 12px">Tüm Linkler</h2>
  <div style="background:#fff;border-radius:10px;overflow:hidden">
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead>
        <tr style="background:#0f172a;color:#e2e8f0">
          <th style="padding:10px 12px;text-align:center">#</th>
          <th style="padding:10px 12px;text-align:left">Kaynak</th>
          <th style="padding:10px 12px;text-align:left">Başlık</th>
          <th style="padding:10px 12px;text-align:left">Durum</th>
          <th style="padding:10px 12px;text-align:center">Kod</th>
          <th style="padding:10px 12px;text-align:left">URL</th>
          <th style="padding:10px 12px;text-align:left">Yönlendirme</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>

  <p style="color:#94a3b8;font-size:12px;text-align:center;margin-top:24px">
    Otomatik oluşturuldu &middot; Video Link Checker
  </p>
</div></body></html>"""

def write_csv(path, urls, results):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["#", "Kaynak", "Ba\u015fl\u0131k", "URL", "Durum", "Kod", "Y\u00f6nlendirme"])
        for i, u in enumerate(urls, 1):
            st, code, r = results.get(u, ("-", "-", ""))
            w.writerow([i, URL_SOURCE.get(u, ""), URL_META.get(u, ""), u, st, code, r])

# ----------------------------- E-posta -------------------------------------
def send_email(html, csv_path, ok, brk, red, total):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER")
    pwd  = os.environ.get("SMTP_PASS")
    to   = os.environ.get("MAIL_TO")
    frm  = os.environ.get("MAIL_FROM") or user

    if not all([host, user, pwd, to]):
        print("[!] SMTP bilgileri eksik, e-posta gonderilmedi.")
        return False

    prefix = os.environ.get("MAIL_SUBJECT_PREFIX", "")
    subj = f"{prefix}Video Link Raporu - {brk} bozuk / {total} link"

    msg = MIMEMultipart("mixed")
    msg["From"] = frm
    msg["To"] = to
    msg["Subject"] = subj

    alt = MIMEMultipart("alternative")
    text = (f"Toplam: {total}\nCalisiyor: {ok}\nBozuk: {brk}\nYonlendi: {red}\n\n"
            f"HTML raporu ve CSV ektedir.")
    alt.attach(MIMEText(text, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)

    with open(csv_path, "rb") as f:
        part = MIMEApplication(f.read(), _subtype="csv")
        part.add_header("Content-Disposition", "attachment",
                        filename="link_raporu.csv")
        msg.attach(part)

    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(user, pwd)
                s.send_message(msg)
        print(f"[+] E-posta gonderildi -> {to}")
        return True
    except Exception as e:
        print(f"[!] E-posta hatasi: {e}")
        return False

# ----------------------------- Ana akis ------------------------------------
def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--json", help="Tek JSON dosya yolu veya URL")
    src.add_argument("--manifest", help="Manifest JSON URL (files[] icerir)")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    t_load_start = time.time()

    if args.manifest:
        urls, _ = asyncio.run(fetch_manifest_sources(args.manifest))
    else:
        print(f"[+] JSON yukleniyor: {args.json}")
        data = load_json(args.json)
        urls = extract_urls(data)
        for u in urls:
            URL_SOURCE[u] = Path(urlparse(args.json).path).name or "main"

    if not urls:
        print("[!] Hic link bulunamadi.")
        sys.exit(0)

    print(f"\n[+] Toplam {len(urls)} link kontrol ediliyor ({CONCURRENT_WORKERS} worker)...")
    t0 = time.time()

    last = [0]
    def progress(done, total):
        if done - last[0] >= 50 or done == total:
            print(f"    {done}/{total}")
            last[0] = done

    results = asyncio.run(run_all(urls, progress))
    elapsed = time.time() - t0

    ok  = sum(1 for u in urls if results[u][0] == OK_STATUS)
    brk = sum(1 for u in urls if results[u][0] == BROKEN_STATUS)
    red = sum(1 for u in urls if results[u][0] == REDIR_STATUS)

    print(f"\n[=] Sonuc: {ok} calisiyor, {brk} bozuk, {red} yonlendi "
          f"({elapsed:.1f} sn)")

    html_path = out / "report.html"
    csv_path  = out / "report.csv"
    summary   = out / "summary.txt"

    html_path.write_text(build_html_report(results, urls, elapsed), encoding="utf-8")
    write_csv(csv_path, urls, results)
    summary.write_text(
        f"Toplam: {len(urls)}\nCalisiyor: {ok}\nBozuk: {brk}\nYonlendi: {red}\n"
        f"Sure: {elapsed:.1f} sn\nTarih: {datetime.now():%Y-%m-%d %H:%M}\n",
        encoding="utf-8")
    print(f"[+] Raporlar: {html_path.name}, {csv_path.name}, {summary.name}")

    gh_sum = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_sum:
        with open(gh_sum, "a", encoding="utf-8") as f:
            f.write(f"## Video Link Raporu\n\n")
            f.write(f"| Toplam | Çalışıyor | Bozuk | Yönlendi |\n")
            f.write(f"|---|---|---|---|\n")
            f.write(f"| {len(urls)} | {ok} | {brk} | {red} |\n\n")
            f.write(f"_Süre: {elapsed:.1f} sn_\n")

    if not args.no_email:
        send_email(html_path.read_text(encoding="utf-8"),
                   str(csv_path), ok, brk, red, len(urls))


if __name__ == "__main__":
    main()
