#!/usr/bin/env python3
"""
Brand Scraper Server — server-side scraping for the AI Brand Scale dashboard.

Fetches a URL, parses the HTML with BeautifulSoup, extracts brand info
(name, product, USP, ICP, description, products list) and returns JSON.

Runs on http://localhost:8766
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HTTPServer = ThreadingHTTPServer  # backward-compat alias
from pathlib import Path

from bs4 import BeautifulSoup

# Load .env for Kie AI key
def load_env():
    env_path = Path(__file__).parent.parent / '.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()
KIE_AI_KEY = os.environ.get('KIE_AI_API_KEY', '')

# ═════════════════════════════════════════════
# ─── AUTH SYSTEM ─────────────────────────────
# ═════════════════════════════════════════════
import hashlib, hmac, secrets

# Postgres data layer. When DATABASE_URL is set, users come from DB;
# otherwise we fall back to .tmp/users.json (local dev convenience).
import db as _db

USERS_FILE = Path(__file__).parent.parent / '.tmp' / 'users.json'
USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
SESSION_SECRET = os.environ.get('SESSION_SECRET', 'abs-session-secret-v1-change-me')

def _load_users():
    if USERS_FILE.exists():
        try: return json.loads(USERS_FILE.read_text(encoding='utf-8'))
        except Exception: return {}
    return {}

def _save_users(users):
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding='utf-8')

def _hash_pw(pw, salt=None):
    if not salt: salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt.encode('utf-8'), 100000)
    return f'{salt}${h.hex()}'

def _verify_pw(pw, stored):
    try:
        salt, _ = stored.split('$', 1)
        return hmac.compare_digest(_hash_pw(pw, salt), stored)
    except Exception: return False

def make_token(user_id):
    """user_id|exp|hmac"""
    import time as _t
    exp = int(_t.time()) + 30*24*3600
    payload = f'{user_id}|{exp}'
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f'{payload}|{sig}'

def verify_token(token):
    if not token: return None
    try:
        import time as _t
        parts = token.split('|')
        if len(parts) != 3: return None
        user_id, exp, sig = parts
        payload = f'{user_id}|{exp}'
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected): return None
        if int(exp) < _t.time(): return None
        return user_id
    except Exception: return None

def auth_user_id(headers):
    auth = (headers.get('Authorization') or '').strip()
    if not auth.startswith('Bearer '): return None
    return verify_token(auth[7:])

def signup_user(email, password, name=''):
    email = (email or '').strip().lower()
    if not email or '@' not in email or len(password) < 6:
        return {'error': 'Невалиден имейл или парола твърде кратка (мин 6 символа)'}
    name = (name or email.split('@')[0])
    password_hash = _hash_pw(password)
    user_id = secrets.token_hex(8)

    if _db.is_enabled():
        ok = _db.user_insert(user_id, email, name, password_hash)
        if not ok:
            return {'error': 'Имейлът вече е регистриран'}
        return {'user': {'id': user_id, 'email': email, 'name': name}, 'token': make_token(user_id)}

    # Filesystem fallback
    users = _load_users()
    if email in users:
        return {'error': 'Имейлът вече е регистриран'}
    users[email] = {
        'id': user_id, 'email': email, 'name': name,
        'password': password_hash,
        'created': int(__import__('time').time()),
    }
    _save_users(users)
    return {'user': {'id': user_id, 'email': email, 'name': name}, 'token': make_token(user_id)}

def login_user(email, password):
    email = (email or '').strip().lower()

    if _db.is_enabled():
        u = _db.user_get_by_email(email)
        if not u or not _verify_pw(password, u['password']):
            return {'error': 'Невалиден имейл или парола'}
        return {'user': {'id': u['id'], 'email': u['email'], 'name': u['name']}, 'token': make_token(u['id'])}

    # Filesystem fallback
    users = _load_users()
    u = users.get(email)
    if not u or not _verify_pw(password, u['password']):
        return {'error': 'Невалиден имейл или парола'}
    return {'user': {'id': u['id'], 'email': u['email'], 'name': u.get('name', '')}, 'token': make_token(u['id'])}

def get_user_by_id(user_id):
    if not user_id: return None

    if _db.is_enabled():
        return _db.user_get_by_id(user_id)

    # Filesystem fallback
    for u in _load_users().values():
        if u.get('id') == user_id:
            return {'id': u['id'], 'email': u['email'], 'name': u.get('name','')}
    return None


# ═════════════════════════════════════════════
# ─── JOB HISTORY (DB-backed, no-op if no DB) ─
# ═════════════════════════════════════════════

def _stage_to_status(state):
    """Map an in-memory job state dict to a normalized history status."""
    if not state: return 'running'
    if state.get('cancelled'): return 'cancelled'
    if state.get('error') or state.get('stage') == 'error': return 'failed'
    stage = state.get('stage', '')
    if stage == 'done': return 'done'
    if stage == 'queued': return 'queued'
    return 'running'


def _job_title(brief):
    if not isinstance(brief, dict): return ''
    brand = (brief.get('brand') or '').strip()
    product = (brief.get('product') or brief.get('product_name') or '').strip()
    if brand and product: return f'{brand} — {product}'
    return brand or product or ''


def _history_record_create(job_id, user_id, feature, brief):
    """Insert a job history row at job creation. No-op if DB not configured or no user_id."""
    if not _db.is_enabled() or not user_id:
        return
    try:
        _db.history_record(
            job_id=job_id,
            user_id=user_id,
            feature=feature,
            title=_job_title(brief),
            brief=brief if isinstance(brief, dict) else None,
            status='queued',
        )
    except Exception as e:
        print(f'[history] record_create failed for {job_id}: {e}', file=sys.stderr)


def _history_sync(job_id, state, feature):
    """Sync a state dict to the history row. Called on every _save_state."""
    if not _db.is_enabled():
        return
    if not state or not state.get('user_id'):
        return
    try:
        status = _stage_to_status(state)
        result = None
        error = None
        if status == 'done':
            # Compact summary for the UI — full state stays on disk
            result = {
                'progress_pct': state.get('progress_pct', 100),
                'completed': state.get('completed', {}),
                'video_url': state.get('video_url') or state.get('final_video'),
                'preview_url': state.get('preview_url') or state.get('html_url'),
            }
        elif status == 'failed':
            error = str(state.get('error') or state.get('stage_label') or 'unknown error')[:500]
        _db.history_update_status(job_id, status, result=result, error=error)
    except Exception as e:
        print(f'[history] sync failed for {job_id}: {e}', file=sys.stderr)

USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)

ITEM_TYPES = [
    # Concrete physical items (high priority — checked first)
    'бутилка', 'стик', 'устройство', 'апарат', 'машина', 'дюза',
    'капсула', 'таблет', 'хапче', 'добавка', 'капки', 'спрей',
    'крем', 'гел', 'мазило', 'серум', 'тоник', 'еликсир',
    'шампоан', 'масло', 'козметика',
    # Foods / drinks
    'напитка', 'прах', 'чай',
    # Categories
    'водородна вода', 'детокс',
]

# Filler/navigation phrases to skip when picking product description
NAV_PHRASES = [
    'прескочи към', 'отваряне на', 'cookie', 'абонирай', 'регистрирай',
    'добави в количка', 'добави към', 'количка', 'търсене', 'меню',
    'влез в', 'създай профил', 'парола', 'safe checkout',
]


def _ascii_safe_url(url):
    """Make a URL fully ASCII-safe (handles Unicode in path/query)."""
    parts = urllib.parse.urlsplit(url)
    # IDNA for hostname, percent-encode path/query
    netloc = parts.netloc
    try:
        if any(ord(c) > 127 for c in (parts.hostname or '')):
            netloc = parts.hostname.encode('idna').decode('ascii')
            if parts.port: netloc += f':{parts.port}'
    except Exception:
        pass
    path = urllib.parse.quote(parts.path, safe="/%-._~!$&'()*+,;=:@")
    query = urllib.parse.quote(parts.query, safe="=&%-._~!$'()*+,;:@/?")
    fragment = urllib.parse.quote(parts.fragment, safe='%-._~')
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, fragment))


def fetch_page(url, timeout=15):
    """Fetch a URL with proper headers and return decoded HTML."""
    safe_url = _ascii_safe_url(url)
    req = urllib.request.Request(
        safe_url,
        headers={
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'bg-BG,bg;q=0.9,en;q=0.8',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = resp.headers.get_content_charset() or 'utf-8'
        try:
            return raw.decode(encoding, errors='replace')
        except LookupError:
            return raw.decode('utf-8', errors='replace')


def get_meta(soup, *names):
    """Get content of meta tag by property or name (first match)."""
    for n in names:
        tag = soup.find('meta', property=n)
        if not tag:
            tag = soup.find('meta', attrs={'name': n})
        if tag and tag.get('content'):
            return tag['content'].strip()
    return ''


def parse_jsonld_products(soup):
    """Extract all Product/Service entries from JSON-LD scripts."""
    products = []
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            text = script.string or script.get_text() or ''
            data = json.loads(text)
        except Exception:
            continue

        items = data if isinstance(data, list) else data.get('@graph', [data])
        for ld in items:
            if not isinstance(ld, dict):
                continue
            t = ld.get('@type', '')
            is_product = (
                t == 'Product' or t == 'Service'
                or (isinstance(t, list) and ('Product' in t or 'Service' in t))
            )
            if not is_product:
                continue
            img = ld.get('image', '')
            if isinstance(img, dict):
                img = img.get('url', '') or img.get('contentUrl', '')
            elif isinstance(img, list) and img:
                img = img[0] if isinstance(img[0], str) else img[0].get('url', '')
            price = ''
            offers = ld.get('offers', {})
            if isinstance(offers, dict):
                price = str(offers.get('price', '') or offers.get('lowPrice', '') or '')
            elif isinstance(offers, list) and offers:
                price = str(offers[0].get('price', '') if isinstance(offers[0], dict) else '')
            products.append({
                'name': (ld.get('name') or '').strip(),
                'description': (ld.get('description') or '').strip()[:400],
                'price': price,
                'image': img or '',
            })
    return products


def extract_icp(text):
    """Pattern-match audience descriptors in Bulgarian body text."""
    if not text:
        return ''
    t = text.lower()
    found = []

    if re.search(r'\bза мъже\b|\bза мъжете\b|\bмъжки\b', t):
        found.append('мъже')
    if re.search(r'\bза жени\b|\bза жените\b|\bженски\b', t):
        found.append('жени')

    age = re.search(r'(\d{2})\s*[\-—–]\s*(\d{2})\s*(?:год|г\.)', t)
    if age:
        found.append(f'{age.group(1)}-{age.group(2)} години')
    elif re.search(r'над\s*(\d{2})\s*г', t):
        m = re.search(r'над\s*(\d{2})\s*г', t)
        found.append(f'над {m.group(1)} години')

    audience_patterns = [
        (r'\bза пушачи\w*\b', 'пушачи'),
        (r'\bза диабет\w+\b', 'диабетици'),
        (r'\bза майки\b', 'майки'),
        (r'\bза студенти\b', 'студенти'),
        (r'\bза спортисти\b', 'спортисти'),
        (r'\bза предприемачи\b', 'предприемачи'),
        (r'\bза професионалисти\b', 'професионалисти'),
        (r'\bза родители\b', 'родители'),
        (r'\bхора с\s+([а-яa-z\s]{3,30})', None),
    ]
    for pat, label in audience_patterns:
        m = re.search(pat, t)
        if m:
            if label:
                found.append(label)
            else:
                found.append(m.group(0).strip()[:60])

    return ', '.join(dict.fromkeys(found))[:300]


def extract_brand_data(html, url):
    """Full extraction of brand data from one page."""
    soup = BeautifulSoup(html, 'html.parser')

    title = (soup.title.string.strip() if soup.title and soup.title.string else '')[:200]
    og_title = get_meta(soup, 'og:title')
    og_desc = get_meta(soup, 'og:description', 'twitter:description')
    og_site = get_meta(soup, 'og:site_name')
    og_image = get_meta(soup, 'og:image')
    og_type = get_meta(soup, 'og:type')
    meta_desc = get_meta(soup, 'description')
    keywords = get_meta(soup, 'keywords')

    is_product_page = (og_type == 'product') or bool(re.search(r'/products?/', url, re.I))

    # ── Body text ──
    for sc in soup(['script', 'style', 'noscript']):
        sc.decompose()
    body_text = re.sub(r'\s+', ' ', soup.get_text(' ', strip=True))[:6000]

    # ── JSON-LD products ──
    products_list = parse_jsonld_products(soup)

    # ── Product item name ──
    product_item = ''
    slug = re.search(r'/products?/([a-z0-9_\-]+)', url, re.I)
    if slug:
        product_item = slug.group(1).replace('-', ' ').replace('_', ' ').title()

    if not product_item and is_product_page:
        h1 = soup.find('h1')
        if h1:
            product_item = re.sub(r'\s+', ' ', h1.get_text(strip=True))[:80]

    if not product_item and products_list:
        product_item = products_list[0].get('name', '')

    if not product_item:
        t = og_title or title
        if og_site:
            t = t.replace(og_site, '')
        product_item = re.split(r'[\|\-—•·]', t)[0].strip()[:80]

    # ── Detect item type from body text ──
    body_lower = body_text.lower()
    found_type = next((it for it in ITEM_TYPES if it in body_lower), '')

    products_text = product_item
    if found_type and found_type not in product_item.lower():
        products_text = f'{product_item} — {found_type}'

    # ── Product function (1-2 sentences) ──
    product_function = ''
    if products_list and products_list[0].get('description'):
        product_function = products_list[0]['description']
    elif is_product_page:
        for p in soup.find_all(['p', 'div'], attrs={'class': re.compile(r'(description|product__|details)', re.I)}):
            txt = re.sub(r'\s+', ' ', p.get_text(strip=True))
            if 60 < len(txt) < 600 and 'cookie' not in txt.lower():
                product_function = txt
                break
        if not product_function:
            for p in soup.find_all('p'):
                txt = re.sub(r'\s+', ' ', p.get_text(strip=True))
                if 60 < len(txt) < 600 and 'cookie' not in txt.lower():
                    product_function = txt
                    break

    if not product_function or any(p in product_function.lower() for p in NAV_PHRASES):
        product_function = og_desc or meta_desc

    # Filter out navigation phrases from function text
    if product_function:
        function_clean = product_function
        for phrase in NAV_PHRASES:
            function_clean = re.sub(re.compile(re.escape(phrase) + r'[^.!?]*[.!?]?', re.I), '', function_clean)
        function_clean = re.sub(r'\s+', ' ', function_clean).strip()
        if function_clean:
            sentences = [s.strip() for s in re.split(r'[.!?]\s+', function_clean) if len(s.strip()) > 15]
            first_two = '. '.join(sentences[:2])[:280]
            if first_two:
                products_text += f'\nФункция: {first_two}'

    # ── Angle / USP from headings (excluding product name) ──
    angle = ''
    product_lower = (product_item or '').lower()
    headings = []
    for tag in ['h1', 'h2', 'h3']:
        for h in soup.find_all(tag):
            txt = re.sub(r'\s+', ' ', h.get_text(strip=True))
            if 15 < len(txt) < 200:
                headings.append(txt)

    skip_words = ('cart', 'меню', 'cookies', 'cookie', 'newsletter', 'абонирай', 'регистрирай')
    for h in headings:
        hl = h.lower()
        if any(s in hl for s in skip_words):
            continue
        if product_lower and product_lower in hl:
            continue
        angle = h
        break

    # ── Brand description ──
    if not is_product_page:
        brand_description = og_desc or meta_desc
    else:
        brand_description = f'{og_site} — {og_desc or meta_desc}'.strip(' —')

    # ── Email, phone ──
    email_match = re.search(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-z]{2,}', html)
    email = email_match.group(0) if email_match else ''
    phone_match = re.search(r'(?:\+359|0)[\s\-]?(?:\d[\s\-]?){8,}', html)
    phone = phone_match.group(0).strip() if phone_match else ''

    # ── Favicon ──
    favicon = ''
    icon_link = soup.find('link', rel=lambda v: v and 'icon' in (v if isinstance(v, str) else ' '.join(v)).lower())
    if icon_link and icon_link.get('href'):
        favicon = urllib.parse.urljoin(url, icon_link['href'])
    if not favicon:
        hostname = urllib.parse.urlparse(url).hostname or ''
        favicon = f'https://www.google.com/s2/favicons?domain={hostname}&sz=128'

    # ── Brand name ──
    name_source = og_site or title or og_title
    name = re.split(r'[\|—•·]| - ', name_source or '')[0].strip()[:60]
    if not name:
        host = urllib.parse.urlparse(url).hostname or ''
        name = host.replace('www.', '').split('.')[0].title()

    return {
        'name': name,
        'website': url,
        'email': email,
        'phone': phone,
        'description': brand_description,
        'favicon': favicon,
        'ogImage': og_image,
        'products': products_text,
        'productsList': products_list,
        'productItemName': product_item,
        'productItemType': found_type,
        'angle': angle,
        'icp': extract_icp(body_text),
        'isProductPage': is_product_page,
        'bodyText': body_text[:3000],
        'keywords': keywords,
    }


def extract_colors(html):
    """Extract brand colors from HTML/CSS."""
    colors = {'primary': '', 'secondary': '', 'accent': '', 'background': ''}
    soup = BeautifulSoup(html, 'html.parser')

    # 1. theme-color meta tag → primary
    theme = soup.find('meta', attrs={'name': 'theme-color'})
    if theme and theme.get('content'):
        colors['primary'] = theme['content'].strip()

    # 2. CSS variables from <style> blocks
    css_text = ' '.join(s.get_text() for s in soup.find_all('style'))
    # Also grab any class="..." style attributes? Skip.

    var_patterns = {
        'primary': [
            r'--primary[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
            r'--color-primary[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
            r'--brand[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
            r'--main-color[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
        ],
        'secondary': [
            r'--secondary[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
            r'--color-secondary[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
        ],
        'accent': [
            r'--accent[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
            r'--color-accent[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
            r'--highlight[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
        ],
        'background': [
            r'--background[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
            r'--bg[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
            r'--color-background[^:]*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
            r'\bbody\s*\{[^}]*background(?:-color)?:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^\)]+\))',
        ],
    }
    for key, patterns in var_patterns.items():
        if colors[key]:
            continue
        for pat in patterns:
            m = re.search(pat, css_text, re.I)
            if m:
                colors[key] = m.group(1).strip()
                break

    # 3. Count hex color frequency as fallback for missing slots
    if not colors['primary'] or not colors['accent']:
        hex_colors = re.findall(r'#([0-9a-fA-F]{6})\b', css_text)
        # Skip near-white, near-black, near-gray
        def is_meaningful(h):
            r, g, b = int(h[:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            mx, mn = max(r, g, b), min(r, g, b)
            return (mx - mn) > 40 and mx > 30 and mn < 230  # not gray, not pure white/black
        freq = {}
        for h in hex_colors:
            h = h.lower()
            if is_meaningful(h):
                freq[h] = freq.get(h, 0) + 1
        sorted_colors = sorted(freq.items(), key=lambda x: -x[1])
        if sorted_colors and not colors['primary']:
            colors['primary'] = '#' + sorted_colors[0][0].upper()
        if len(sorted_colors) > 1 and not colors['accent']:
            colors['accent'] = '#' + sorted_colors[1][0].upper()

    # 4. Defaults
    if not colors['secondary']:
        colors['secondary'] = '#000000'
    if not colors['background']:
        colors['background'] = '#FFFFFF'

    # Normalize all colors to hex
    for k, v in colors.items():
        if v.startswith('rgb'):
            m = re.match(r'rgba?\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)', v)
            if m:
                colors[k] = '#{:02X}{:02X}{:02X}'.format(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif v.startswith('#') and len(v) == 4:
            # #abc → #aabbcc
            colors[k] = '#' + ''.join(c*2 for c in v[1:]).upper()
        elif v.startswith('#'):
            colors[k] = v.upper()

    return colors


def analyze_with_gemini(brand_name, url, scraped_text):
    """Send rich content to Gemini 2.5 Pro for Bulgarian brand analysis."""
    if not KIE_AI_KEY:
        return None

    prompt = (
        f'Анализирай това съдържание от уебсайт на бранда "{brand_name}" ({url}) и генерирай подробен профил на български език.\n\n'
        f'СЪДЪРЖАНИЕ ОТ САЙТА:\n{scraped_text[:12000]}\n\n'
        'Върни САМО валиден JSON, без markdown, без обяснения:\n'
        '{\n'
        '  "brand_description": "1 голям параграф (3-5 изречения) описващ бранда — какво продава, мисия, ценности, тон. На български.",\n'
        '  "target_audience": "1 голям параграф (3-5 изречения) — Кои са идеалните клиенти: демография, психография, болки, аспирации. На български.",\n'
        '  "features_benefits": [\n'
        '    {"title": "Заглавие", "feature": "Описание на функцията", "benefit": "Полза за клиента"}\n'
        '  ],\n'
        '  "main_angle": "Кратък USP / маркетингов ъгъл (1-2 изречения)",\n'
        '  "tone_of_voice": "Тон на комуникация (1 изречение)"\n'
        '}\n\n'
        'ВАЖНО: всичко на български, конкретно, реални данни от съдържанието. features_benefits — върни 5-9 елемента. НЕ измисляй информация.'
    )

    try:
        req_body = json.dumps({
            'model': 'gemini-2.5-pro',
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.3,
        }).encode('utf-8')

        req = urllib.request.Request(
            'https://api.kie.ai/gemini-2.5-pro/v1/chat/completions',
            data=req_body,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {KIE_AI_KEY}',
                'User-Agent': 'BrandScraper/1.0',
                'Accept': 'application/json',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
        # Strip markdown fences
        content = re.sub(r'```json\s*|\s*```', '', content).strip()
        match = re.search(r'\{[\s\S]*\}', content)
        if match:
            return json.loads(match.group(0))
    except Exception as e:
        print(f'[Gemini analysis] {e}', file=sys.stderr)
    return None


def find_about_url(soup, base_url):
    """Find link to About / About Us / За нас page."""
    keywords = ['about', 'about-us', 'za-nas', 'about_us', 'company', 'mission', 'нас']
    for link in soup.find_all('a', href=True):
        href = link['href'].lower()
        text = link.get_text(strip=True).lower()
        if any(k in href for k in keywords) or any(k in text for k in ['за нас', 'about', 'мисия', 'компания']):
            return urllib.parse.urljoin(base_url, link['href'])
    return None


def deep_scrape(url):
    """Fetch homepage + product + about, extract colors, optionally AI-enrich."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        url = 'https://' + url
        parsed = urllib.parse.urlparse(url)
    origin = f'{parsed.scheme}://{parsed.netloc}'
    is_product_page = bool(re.search(r'/products?/', parsed.path, re.I))

    home_data = None
    product_data = None
    about_data = None
    home_html = ''
    product_html = ''
    about_html = ''
    product_url = url if is_product_page else None
    about_url = None

    # 1. Homepage
    try:
        home_html = fetch_page(origin)
        home_data = extract_brand_data(home_html, origin)
        soup = BeautifulSoup(home_html, 'html.parser')
        if not product_url:
            link = soup.find('a', href=re.compile(r'/products?/', re.I))
            if link and link.get('href'):
                product_url = urllib.parse.urljoin(origin, link['href'])
        about_url = find_about_url(soup, origin)
    except Exception as e:
        print(f'[homepage] {e}', file=sys.stderr)

    # 2. Product page
    if product_url:
        try:
            product_html = fetch_page(product_url)
            product_data = extract_brand_data(product_html, product_url)
        except Exception as e:
            print(f'[product page] {e}', file=sys.stderr)

    # 3. About page
    if about_url:
        try:
            about_html = fetch_page(about_url)
            about_data = extract_brand_data(about_html, about_url)
        except Exception as e:
            print(f'[about page] {e}', file=sys.stderr)

    # Fallback — original URL only
    if not home_data and not product_data:
        html = fetch_page(url)
        home_html = html
        home_data = extract_brand_data(html, url)

    # ── Merge ──
    base = dict(home_data) if home_data else dict(product_data or {})
    if product_data:
        if product_data.get('products'):
            base['products'] = product_data['products']
        if product_data.get('productsList'):
            base['productsList'] = product_data['productsList']
        if product_data.get('productItemName'):
            base['productItemName'] = product_data['productItemName']
        if product_data.get('productItemType'):
            base['productItemType'] = product_data['productItemType']
        if product_data.get('ogImage') and not base.get('ogImage'):
            base['ogImage'] = product_data['ogImage']

    base['website'] = url

    # ── Colors from CSS (homepage primarily) ──
    base['colors'] = extract_colors(home_html or product_html or about_html)

    # Combine all body text for AI analysis
    combined_text = ' '.join([
        (home_data or {}).get('bodyText', ''),
        (product_data or {}).get('bodyText', ''),
        (about_data or {}).get('bodyText', ''),
    ])[:15000]

    # Extract EIK / Bulstat / company info from footer/contact pages
    base['companyInfo'] = extract_company_info(home_html + ' ' + product_html + ' ' + about_html)

    # ── AI enrichment via Gemini (if key available) ──
    ai_data = analyze_with_gemini(base.get('name', ''), url, combined_text)
    if ai_data:
        base['brandDescription'] = ai_data.get('brand_description', '') or base.get('description', '')
        base['targetAudience'] = ai_data.get('target_audience', '') or base.get('icp', '')
        base['featuresBenefits'] = ai_data.get('features_benefits', [])
        base['mainAngle'] = ai_data.get('main_angle', '') or base.get('angle', '')
        base['toneOfVoice'] = ai_data.get('tone_of_voice', '')
        # Promote AI angle to main angle field too
        if ai_data.get('main_angle'):
            base['angle'] = ai_data['main_angle']
    else:
        base['brandDescription'] = base.get('description', '')
        base['targetAudience'] = base.get('icp', '')
        base['featuresBenefits'] = []
        base['mainAngle'] = base.get('angle', '')
        base['toneOfVoice'] = ''

    # Cleanup
    base['icp'] = base['targetAudience']  # backward-compat
    base.pop('bodyText', None)
    return base


def extract_company_info(text):
    """Extract Bulgarian company registration info (ЕИК, Булстат) from page text."""
    info = {'eik': '', 'companyName': '', 'ownerName': ''}

    # ЕИК / Булстат (9-13 digit Bulgarian company ID)
    eik_match = re.search(r'(?:ЕИК|Булстат|БУЛСТАТ|EIK)[\s:]*([0-9]{9,13})', text, re.I)
    if eik_match:
        info['eik'] = eik_match.group(1)

    # Company name (look for ООД, ЕООД, АД, ЕТ)
    company_match = re.search(r'([А-ЯA-Z][А-Яа-яA-Za-z\s\-]{2,50})\s+(ООД|ЕООД|АД|ЕТ|EOOD|OOD|AD)\b', text)
    if company_match:
        info['companyName'] = (company_match.group(1) + ' ' + company_match.group(2)).strip()

    return info


# ═════════════════════════════════════════════
# ─── AD TEMPLATES (Statics inspiration) ──────
# ═════════════════════════════════════════════
AD_TEMPLATES_DIR = Path(__file__).parent.parent / 'ad_templates'

AD_TEMPLATE_CATEGORIES = [
    {'id':'all',                     'label':'All'},
    {'id':'product-focused',         'label':'Product Focus'},
    {'id':'features-benefits',       'label':'Features & Benefits'},
    {'id':'problem-solution',        'label':'Problem-Solution'},
    {'id':'comparison',              'label':'Comparison'},
    {'id':'infographic-educational', 'label':'Infographic/Educational'},
    {'id':'lifestyle',               'label':'Lifestyle'},
    {'id':'testimonial',             'label':'Testimonial/Review'},
    {'id':'promo-offer',             'label':'Promotional/Offer'},
    {'id':'media-news',              'label':'Media/News'},
    {'id':'collage',                 'label':'Collage'},
]

def list_ad_templates(category=None):
    """List all ad template thumbnails grouped by category."""
    if not AD_TEMPLATES_DIR.exists():
        return {'categories': AD_TEMPLATE_CATEGORIES, 'templates': []}
    templates = []
    cats_to_scan = [category] if category and category != 'all' else [c['id'] for c in AD_TEMPLATE_CATEGORIES if c['id'] != 'all']
    if category == 'all' or not category:
        # Show all
        for cat in cats_to_scan:
            cat_dir = AD_TEMPLATES_DIR / cat
            if not cat_dir.exists(): continue
            for f in sorted(cat_dir.iterdir()):
                if f.suffix.lower() in {'.webp','.jpg','.jpeg','.png'}:
                    templates.append({'category': cat, 'filename': f.name, 'url': f'/ad-templates/{cat}/{f.name}'})
    else:
        cat_dir = AD_TEMPLATES_DIR / category
        if cat_dir.exists():
            for f in sorted(cat_dir.iterdir()):
                if f.suffix.lower() in {'.webp','.jpg','.jpeg','.png'}:
                    templates.append({'category': category, 'filename': f.name, 'url': f'/ad-templates/{category}/{f.name}'})
    return {'categories': AD_TEMPLATE_CATEGORIES, 'templates': templates}


# ═════════════════════════════════════════════
# ─── ADVERTORIAL STUDIO ──────────────────────
# ═════════════════════════════════════════════
import threading, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
ADV_JOBS = {}
ADV_JOBS_DIR = Path(__file__).parent.parent / '.tmp' / 'advertorial_jobs'
ADV_JOBS_DIR.mkdir(parents=True, exist_ok=True)

ADV_LENGTH_CONFIG = {
    'short':  {'words': 700,  'images': 5,  'testimonials': 6},
    'medium': {'words': 1500, 'images': 10, 'testimonials': 9},
    'long':   {'words': 3000, 'images': 18, 'testimonials': 12},
}

ADV_ANGLE_TYPES = [
    'personal_discovery', 'expert_reveal', 'news_investigation', 'mistake_hook',
    'hidden_truth', 'transformation', 'comparison', 'mechanism_reveal',
    'cost_of_inaction', 'identity',
]


def adv_generate_angles(brand, product_name, product_desc, audience, brand_doc):
    """Generate 10 advertorial angles via Gemini."""
    prompt = (
        f'Generate 10 distinct advertorial angles for a Bulgarian Shopify-style article.\n\n'
        f'BRAND: {brand}\nPRODUCT: {product_name}\nDESCRIPTION: {product_desc}\n'
        f'AUDIENCE: {audience}\nBRAND DOC: {brand_doc[:2000]}\n\n'
        f'Return ONLY a JSON array of 10 angle objects, each with:\n'
        f'{{"type": "personal_discovery|expert_reveal|news_investigation|mistake_hook|hidden_truth|transformation|comparison|mechanism_reveal|cost_of_inaction|identity",\n'
        f' "headline": "Bold compelling Bulgarian headline (10-14 words)",\n'
        f' "hook": "1-sentence opening hook (Bulgarian)",\n'
        f' "promise": "1-sentence promise (Bulgarian)",\n'
        f' "audience_fit": "1-sentence why this resonates with the audience (Bulgarian)"}}\n\n'
        f'Each angle should use a DIFFERENT type. All text on Bulgarian.\n'
        f'NO emojis. NO clichés like "разтапящо".\n'
        f'Return only valid JSON array.'
    )
    text = kie_claude_complete(prompt, 4000)
    text = re.sub(r'```json\s*|\s*```', '', text).strip()
    m = re.search(r'\[[\s\S]*\]', text)
    if not m:
        raise RuntimeError('No JSON array in response')
    return json.loads(m.group(0))


def adv_generate_content(brief):
    """Call Claude/Gemini to write full delimited advertorial content."""
    cfg = ADV_LENGTH_CONFIG.get(brief.get('length','medium'), ADV_LENGTH_CONFIG['medium'])
    angle = brief.get('angle', {})
    prompt = (
        f'Ти си експерт копирайтър. Напиши пълна Shopify-style advertorial статия на български.\n\n'
        f'БРАНД: {brief.get("brand")}\nПРОДУКТ: {brief.get("product_name")}\n'
        f'ОПИСАНИЕ: {brief.get("product_desc","")}\nАУДИТОРИЯ: {brief.get("audience","")}\n'
        f'BRAND DOC: {(brief.get("brand_doc") or "")[:3000]}\n\n'
        f'ИЗБРАН ANGLE:\nТип: {angle.get("type")}\nHeadline: {angle.get("headline")}\nHook: {angle.get("hook")}\n'
        f'Promise: {angle.get("promise")}\n\n'
        f'ДЪЛЖИНА: ~{cfg["words"]} думи, {cfg["images"]} inline images, {cfg["testimonials"]} testimonials\n'
        f'BRAND COLOR: {brief.get("brand_color","#16a34a")}\nCTA URL: {brief.get("cta_url","#")}\n\n'
        f'Върни ТОЧНО следния формат с === delimiters (без markdown fences):\n\n'
        f'===META===\n'
        f'publication: <българско име на здравна/lifestyle публикация>\n'
        f'publish_date: <днешна дата на български>\n'
        f'topbar_text: <кратък промо текст>\n'
        f'headline: <главно заглавие, с [HL]ключова фраза[/HL] за accent>\n'
        f'subheadline: <под-заглавие>\n'
        f'stars_text: <текст до 5 звезди, напр. "Над X доволни клиенти">\n'
        f'primary_cta_text: <CTA бутон текст>\n'
        f'cta_meta_text: <под CTA, напр. "60-дневна гаранция · Безплатна доставка">\n'
        f'asseen_label: ПОКАЗАН В\n'
        f'guarantee_title: <гаранция заглавие>\n'
        f'guarantee_text: <1-2 изречения с **bold** ключова фраза>\n'
        f'author_name: <българско лекарско/експертно име>\n'
        f'author_title: <Кардиолог, 27 г. стаж / подобно>\n'
        f'author_bio: <2-3 изречения биография>\n'
        f'final_headline: <финално CTA заглавие>\n'
        f'final_text: <1 изречение urgency>\n'
        f'sticky_text: <кратък текст за sticky bar>\n'
        f'sticky_cta_text: <Поръчай / similar>\n'
        f'disclaimer: <disclaimer текст>\n'
        f'asseen_logos: <4 имена на публикации, разделени с | >\n'
        f'===END_META===\n\n'
        f'===HERO===\n'
        f'hero_prompt: <Photo-realistic editorial prompt на английски, за главна снимка с продукта>\n'
        f'hero_aspect: 16:9\n'
        f'hero_shows_product: true\n'
        f'===END_HERO===\n\n'
        f'===IMAGES===\n'
        f'[img1]\n'
        f'prompt: <Editorial photo prompt EN>\n'
        f'aspect: 16:9 | 4:3 | 1:1\n'
        f'caption: <българско caption>\n'
        f'shows_product: true|false\n\n'
        f'[img2]\n'
        f'... ({cfg["images"]} images total)\n'
        f'===END_IMAGES===\n\n'
        f'===STATS===\n'
        f'<num> | <label>\n'
        f'<num> | <label>\n'
        f'<num> | <label>\n'
        f'===END_STATS===\n\n'
        f'===HOW===\n'
        f'<step_title> | <step_text>\n'
        f'<step_title> | <step_text>\n'
        f'<step_title> | <step_text>\n'
        f'===END_HOW===\n\n'
        f'===TESTIMONIALS===\n'
        f'[1]\n'
        f'name: <българско име>\n'
        f'age: <число>\n'
        f'location: <град>\n'
        f'rating: 5\n'
        f'verified: true\n'
        f'text: <2-3 изречения Trustpilot-style review>\n\n'
        f'[2]\n... ({cfg["testimonials"]} testimonials total)\n'
        f'===END_TESTIMONIALS===\n\n'
        f'===BODY===\n'
        f'Тук пишеш ПЪЛНАТА статия на български (~{cfg["words"]} думи).\n'
        f'Използвай:\n'
        f'- **bold** за акценти\n'
        f'- *italic* за nuance\n'
        f'- ## Heading за подзаглавия\n'
        f'- [[IMG:img1]], [[IMG:img2]] и т.н. маркери на правилните места в текста\n'
        f'- [PULLQUOTE]Силна цитат[/PULLQUOTE] за акцент\n'
        f'- [CALLOUT title="Заглавие"]Съдържание[/CALLOUT] за box\n'
        f'- [CTA-BLOCK title="..." text="..."] 2-3 пъти в текста\n'
        f'- [text](#cta) за inline links към CTA\n'
        f'Всички {cfg["images"]} images маркери ТРЯБВА да са в текста.\n'
        f'===END_BODY===\n'
    )
    return kie_claude_complete(prompt, 16000)


def adv_parse_delimited(text):
    """Parse ===SECTION=== blocks into dict."""
    out = {'images': [], 'testimonials': [], 'stats': [], 'how_steps': [], 'asseen_logos': []}

    def get_block(name):
        m = re.search(rf'==={name}===\s*(.*?)(?:===END_{name}===|===[A-Z_]+===|\Z)', text, re.S)
        return m.group(1).strip() if m else ''

    def parse_kv(block):
        d = {}
        for line in block.splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                d[k.strip()] = v.strip()
        return d

    meta = parse_kv(get_block('META'))
    out.update(meta)
    if 'asseen_logos' in meta:
        out['asseen_logos'] = [s.strip() for s in meta['asseen_logos'].split('|') if s.strip()]

    hero = parse_kv(get_block('HERO'))
    out['hero_image_prompt'] = hero.get('hero_prompt', '')
    out['hero_image_aspect'] = hero.get('hero_aspect', '16:9')
    out['hero_shows_product'] = str(hero.get('hero_shows_product','true')).lower() == 'true'

    # Images
    img_block = get_block('IMAGES')
    for m in re.finditer(r'\[(img\d+)\]\s*(.*?)(?=\[img\d+\]|\Z)', img_block, re.S):
        kv = parse_kv(m.group(2))
        out['images'].append({
            'id': m.group(1),
            'prompt': kv.get('prompt',''),
            'aspect': kv.get('aspect','16:9'),
            'caption': kv.get('caption',''),
            'shows_product': str(kv.get('shows_product','false')).lower() == 'true',
        })

    # Stats
    for line in get_block('STATS').splitlines():
        if '|' in line:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 2: out['stats'].append({'num': parts[0], 'label': parts[1]})

    # How steps
    for line in get_block('HOW').splitlines():
        if '|' in line:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 2: out['how_steps'].append({'title': parts[0], 'text': parts[1] if len(parts) > 1 else ''})

    # Testimonials
    test_block = get_block('TESTIMONIALS')
    for m in re.finditer(r'\[(\d+)\]\s*(.*?)(?=\[\d+\]|\Z)', test_block, re.S):
        kv = parse_kv(m.group(2))
        out['testimonials'].append({
            'name': kv.get('name',''),
            'age': kv.get('age',''),
            'location': kv.get('location',''),
            'rating': int(kv.get('rating','5') or '5'),
            'verified': str(kv.get('verified','true')).lower() == 'true',
            'text': kv.get('text',''),
        })

    out['body_markdown'] = get_block('BODY')
    return out


def adv_render_body(md, images_by_id, brand_color, cta_url):
    """Convert lightweight markdown body to HTML."""
    if not md: return ''
    html = md
    # Inline IMG
    def img_repl(m):
        img_id = m.group(1)
        img = images_by_id.get(img_id, {})
        cap = img.get('caption','')
        return f'<figure class="adv-fig"><img src="images/{img_id}.jpg" alt="{cap}"><figcaption>{cap}</figcaption></figure>'
    html = re.sub(r'\[\[IMG:(\w+)\]\]', img_repl, html)
    # Pullquote
    html = re.sub(r'\[PULLQUOTE\]([\s\S]*?)\[/PULLQUOTE\]', r'<blockquote class="adv-pullquote">\1</blockquote>', html)
    # Callout
    html = re.sub(r'\[CALLOUT\s+title="([^"]+)"\]([\s\S]*?)\[/CALLOUT\]',
                  r'<div class="adv-callout"><div class="adv-callout-title">\1</div><p>\2</p></div>', html)
    # CTA-BLOCK
    def cta_repl(m):
        title = m.group(1); text = m.group(2)
        return f'<div class="adv-cta-block"><div class="adv-cta-title">{title}</div><div class="adv-cta-text">{text}</div><a class="adv-cta-btn" href="{cta_url}">КУПИ СЕГА →</a></div>'
    html = re.sub(r'\[CTA-BLOCK\s+title="([^"]+)"\s+text="([^"]+)"\]', cta_repl, html)
    # Headings
    html = re.sub(r'(?m)^###\s+(.+)$', r'<h3>\1</h3>', html)
    html = re.sub(r'(?m)^##\s+(.+)$', r'<h2>\1</h2>', html)
    # Bold, italic, links
    html = re.sub(r'\*\*([^\*]+)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'(?<!\*)\*([^\*\n]+)\*(?!\*)', r'<em>\1</em>', html)
    html = re.sub(r'\[([^\]]+)\]\(#cta\)', rf'<a href="{cta_url}" class="adv-inline-cta">\1</a>', html)
    html = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', r'<a href="\2" target="_blank" rel="noopener">\1</a>', html)
    # Paragraphs
    paras = [p.strip() for p in html.split('\n\n') if p.strip()]
    wrapped = []
    for p in paras:
        if p.startswith('<') and not p.startswith('<strong') and not p.startswith('<em') and not p.startswith('<a '):
            wrapped.append(p)
        else:
            wrapped.append(f'<p>{p}</p>')
    return '\n'.join(wrapped)


def adv_render_html(parsed, brief, job_id):
    """Render the full self-contained Shopify-style advertorial HTML."""
    brand_color = brief.get('brand_color', '#16a34a')
    def hex_darken(h, amt=0.78):
        h = h.lstrip('#')
        r, g, b = [int(h[i:i+2], 16) for i in (0, 2, 4)]
        return '#{:02X}{:02X}{:02X}'.format(int(r*amt), int(g*amt), int(b*amt))
    def hex_soften(h):
        h = h.lstrip('#')
        r, g, b = [int(h[i:i+2], 16) for i in (0, 2, 4)]
        return '#{:02X}{:02X}{:02X}'.format(min(255,r+(255-r)*92//100), min(255,g+(255-g)*92//100), min(255,b+(255-b)*92//100))
    brand_dark = hex_darken(brand_color)
    brand_soft = hex_soften(brand_color)

    images_by_id = {im['id']: im for im in parsed.get('images', [])}
    headline = parsed.get('headline','').replace('[HL]', '<span class="hl">').replace('[/HL]', '</span>')

    # Strip markers for images that didn't generate (file doesn't exist)
    images_dir = ADV_JOBS_DIR / job_id / 'images'
    available = set()
    if images_dir.exists():
        for f in images_dir.glob('*.jpg'):
            available.add(f.stem)
    body_md = parsed.get('body_markdown','')
    for img_id in list(images_by_id.keys()):
        if img_id not in available:
            body_md = re.sub(rf'\[\[IMG:{re.escape(img_id)}\]\]\s*\n?', '', body_md)
    # Also handle hero image missing
    if 'hero' not in available:
        # Will show broken image — fallback handled in template via onerror

        pass

    cta_url = brief.get('cta_url', '#')
    body_html = adv_render_body(body_md, images_by_id, brand_color, cta_url)

    asseen = parsed.get('asseen_logos') or ['Здравна Линия', 'BG Wellness', 'Жената Днес', 'Бизнес и Здраве']

    # Testimonials grid
    test_html = ''
    for t in parsed.get('testimonials', []):
        initials = ''.join([w[0] for w in (t.get('name','?')).split()[:2]]).upper()
        verified = '<span class="adv-verified">✓ Verified</span>' if t.get('verified') else ''
        stars = '★' * int(t.get('rating', 5))
        test_html += f'''<div class="adv-test-card"><div class="adv-test-head"><div class="adv-test-avatar">{initials}</div><div><div class="adv-test-name">{t.get('name','')}{verified}</div><div class="adv-test-meta">{t.get('age','')} · {t.get('location','')}</div></div></div><div class="adv-test-stars">{stars}</div><div class="adv-test-text">{t.get('text','')}</div></div>'''

    stats_html = ''.join(f'<div class="adv-stat"><div class="adv-stat-num">{s["num"]}</div><div class="adv-stat-label">{s["label"]}</div></div>' for s in parsed.get('stats', []))

    how_html = ''
    for i, step in enumerate(parsed.get('how_steps', []), 1):
        how_html += f'<div class="adv-how-step"><div class="adv-how-num">{i}</div><div class="adv-how-title">{step.get("title","")}</div><div class="adv-how-text">{step.get("text","")}</div></div>'

    asseen_html = ''.join(f'<span class="adv-asseen-item">{name}</span>' for name in asseen)

    template = f'''<!DOCTYPE html>
<html lang="bg">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{headline}</title>
<style>
:root {{
  --brand: {brand_color};
  --brand-dark: {brand_dark};
  --brand-soft: {brand_soft};
  --serif: 'Charter', 'Georgia', 'Iowan Old Style', 'Sitka Text', serif;
  --sans: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', Roboto, sans-serif;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: var(--sans); color: #1a1a1a; line-height: 1.65; background: #fdfdfd; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }}
.adv-topbar {{ background: #1a1a1a; color: #fff; text-align: center; padding: 10px 16px; font-size: 12.5px; font-weight: 600; letter-spacing: 0.03em; }}
.adv-header {{ max-width: 720px; margin: 0 auto; padding: 20px 20px 18px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #eaeaea; }}
.adv-pub {{ font-family: var(--serif); font-weight: 700; font-size: 19px; letter-spacing: -0.02em; }}
.adv-disclosure {{ font-size: 10.5px; color: #999; text-transform: uppercase; letter-spacing: 0.12em; font-weight: 600; }}
.adv-hero {{ max-width: 720px; margin: 0 auto; padding: 40px 20px 24px; text-align: center; }}
.adv-hero h1 {{ font-family: var(--serif); font-size: 44px; font-weight: 700; letter-spacing: -0.025em; line-height: 1.12; margin-bottom: 18px; color: #0f0f0f; }}
.adv-hero h1 .hl {{ color: var(--brand); font-style: italic; }}
.adv-hero .adv-sub {{ font-size: 19px; color: #555; line-height: 1.55; margin-bottom: 22px; max-width: 600px; margin-left: auto; margin-right: auto; }}
.adv-stars {{ display: inline-flex; align-items: center; gap: 8px; background: #f0fdf4; color: #15803d; padding: 8px 16px; border-radius: 100px; font-size: 13px; font-weight: 700; margin-bottom: 24px; border: 1px solid #bbf7d0; }}
.adv-stars-icons {{ color: #16a34a; letter-spacing: 1px; font-size: 14px; }}
.adv-hero-img {{ width: 100%; max-width: 720px; aspect-ratio: 16/9; object-fit: cover; border-radius: 14px; margin: 20px 0 24px; box-shadow: 0 8px 32px rgba(0,0,0,0.08); }}
.adv-cta {{ display: inline-block; background: var(--brand); color: #fff !important; padding: 18px 38px; font-size: 16.5px; font-weight: 800; text-decoration: none; border-radius: 10px; box-shadow: 0 8px 20px rgba(0,0,0,0.14), 0 0 0 1px rgba(0,0,0,0.05); transition: transform .18s ease, box-shadow .18s ease; text-transform: uppercase; letter-spacing: 0.04em; }}
.adv-cta:hover {{ transform: translateY(-2px); box-shadow: 0 12px 28px rgba(0,0,0,0.20); }}
.adv-cta-meta {{ margin-top: 12px; font-size: 12.5px; color: #888; font-weight: 500; }}
.adv-asseen {{ max-width: 720px; margin: 32px auto; padding: 22px 20px; border-top: 1px solid #eaeaea; border-bottom: 1px solid #eaeaea; text-align: center; }}
.adv-asseen-label {{ font-size: 10.5px; color: #999; letter-spacing: 0.16em; font-weight: 700; margin-bottom: 14px; }}
.adv-asseen-row {{ display: flex; justify-content: center; gap: 32px; flex-wrap: wrap; font-family: var(--serif); font-size: 15px; color: #aaa; font-weight: 600; font-style: italic; }}
.adv-body {{ max-width: 720px; margin: 0 auto; padding: 30px 20px; font-family: var(--serif); font-size: 19px; line-height: 1.7; color: #2a2a2a; }}
.adv-body p {{ margin-bottom: 22px; }}
.adv-body p:first-of-type::first-letter {{ font-size: 56px; font-weight: 700; float: left; line-height: 0.9; margin: 6px 8px 0 0; color: var(--brand); }}
.adv-body h2 {{ font-family: var(--serif); font-size: 30px; font-weight: 700; letter-spacing: -0.015em; margin: 36px 0 16px; line-height: 1.25; color: #0f0f0f; }}
.adv-body h3 {{ font-family: var(--serif); font-size: 23px; font-weight: 600; margin: 28px 0 12px; }}
.adv-body strong {{ font-weight: 700; }}
.adv-body em {{ font-style: italic; }}
.adv-body a {{ color: var(--brand); text-decoration: underline; }}
.adv-fig {{ margin: 22px 0; }}
.adv-fig img {{ width: 100%; border-radius: 10px; }}
.adv-fig figcaption {{ font-size: 12.5px; color: #888; text-align: center; margin-top: 6px; font-style: italic; }}
.adv-pullquote {{ font-size: 22px; font-weight: 600; font-style: italic; color: var(--brand-dark); border-left: 4px solid var(--brand); padding: 14px 22px; margin: 26px 0; line-height: 1.4; }}
.adv-callout {{ background: var(--brand-soft); border-left: 4px solid var(--brand); border-radius: 8px; padding: 18px 22px; margin: 22px 0; }}
.adv-callout-title {{ font-weight: 800; font-size: 16px; color: var(--brand-dark); margin-bottom: 6px; }}
.adv-cta-block {{ background: var(--brand-soft); border: 2px solid var(--brand); border-radius: 12px; padding: 24px; margin: 26px 0; text-align: center; }}
.adv-cta-title {{ font-size: 20px; font-weight: 800; color: var(--brand-dark); margin-bottom: 6px; }}
.adv-cta-text {{ font-size: 15px; color: #444; margin-bottom: 16px; }}
.adv-cta-btn {{ display: inline-block; background: var(--brand); color: #fff !important; padding: 14px 32px; font-size: 15px; font-weight: 800; text-decoration: none; border-radius: 8px; text-transform: uppercase; letter-spacing: 0.02em; }}
.adv-inline-cta {{ color: var(--brand) !important; font-weight: 700; }}
.adv-stats {{ max-width: 720px; margin: 40px auto; padding: 0 20px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; text-align: center; }}
.adv-stat {{ padding: 22px 14px; background: linear-gradient(180deg, #fafafa, #fff); border-radius: 12px; border: 1px solid #eee; }}
.adv-stat-num {{ font-size: 34px; font-weight: 800; color: var(--brand); letter-spacing: -0.028em; line-height: 1; }}
.adv-stat-label {{ font-size: 12.5px; color: #666; margin-top: 8px; font-weight: 500; }}
.adv-how {{ max-width: 720px; margin: 48px auto; padding: 0 20px; }}
.adv-how h2 {{ font-family: var(--serif); font-size: 32px; font-weight: 700; text-align: center; margin-bottom: 32px; letter-spacing: -0.018em; }}
.adv-how-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 22px; }}
.adv-how-step {{ text-align: center; padding: 8px; }}
.adv-how-num {{ width: 60px; height: 60px; border-radius: 50%; background: var(--brand); color: #fff; display: flex; align-items: center; justify-content: center; font-size: 26px; font-weight: 800; margin: 0 auto 14px; box-shadow: 0 6px 16px rgba(0,0,0,0.12); }}
.adv-how-title {{ font-weight: 700; font-size: 17px; margin-bottom: 8px; color: #1a1a1a; }}
.adv-how-text {{ font-size: 14.5px; color: #555; line-height: 1.55; }}
.adv-testimonials {{ max-width: 720px; margin: 48px auto; padding: 0 20px; }}
.adv-testimonials h2 {{ font-family: var(--serif); font-size: 32px; font-weight: 700; text-align: center; margin-bottom: 28px; letter-spacing: -0.018em; }}
.adv-test-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }}
.adv-test-card {{ background: #fff; border: 1px solid #eaeaea; border-radius: 12px; padding: 18px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); transition: transform .15s, box-shadow .15s; }}
.adv-test-card:hover {{ transform: translateY(-2px); box-shadow: 0 6px 18px rgba(0,0,0,0.08); }}
.adv-test-head {{ display: flex; gap: 11px; align-items: center; margin-bottom: 10px; }}
.adv-test-avatar {{ width: 42px; height: 42px; border-radius: 50%; background: linear-gradient(135deg, var(--brand-soft), #fff); color: var(--brand-dark); display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 13.5px; border: 1.5px solid var(--brand-soft); }}
.adv-test-name {{ font-weight: 700; font-size: 14px; color: #1a1a1a; }}
.adv-verified {{ background: #16a34a; color: #fff; font-size: 9.5px; padding: 2px 7px; border-radius: 100px; font-weight: 700; margin-left: 7px; letter-spacing: 0.04em; }}
.adv-test-meta {{ font-size: 11.5px; color: #999; margin-top: 1px; }}
.adv-test-stars {{ color: #f59e0b; font-size: 14px; margin-bottom: 8px; letter-spacing: 1px; }}
.adv-test-text {{ font-size: 13.5px; color: #444; line-height: 1.55; font-family: var(--serif); }}
.adv-guarantee {{ max-width: 720px; margin: 40px auto; padding: 28px 32px; background: linear-gradient(135deg, #fffbeb, #fef3c7); border-radius: 14px; text-align: center; border: 1px solid #fcd34d; box-shadow: 0 4px 12px rgba(251,191,36,0.12); }}
.adv-guarantee-shield {{ font-size: 36px; margin-bottom: 8px; }}
.adv-guarantee-title {{ font-family: var(--serif); font-size: 24px; font-weight: 700; color: #92400e; letter-spacing: 0.01em; margin-bottom: 10px; }}
.adv-guarantee-text {{ font-size: 15.5px; color: #78350f; line-height: 1.55; max-width: 540px; margin: 0 auto; }}
.adv-author {{ max-width: 720px; margin: 36px auto; padding: 24px 26px; background: #fafafa; border-left: 4px solid var(--brand); border-radius: 10px; }}
.adv-author-name {{ font-family: var(--serif); font-weight: 700; font-size: 18px; color: var(--brand-dark); }}
.adv-author-title {{ font-size: 12.5px; color: #888; margin-bottom: 8px; font-weight: 500; }}
.adv-author-bio {{ font-size: 14.5px; color: #444; line-height: 1.6; font-family: var(--serif); }}
.adv-final {{ background: linear-gradient(135deg, var(--brand), var(--brand-dark)); color: #fff; padding: 56px 24px; text-align: center; margin-top: 40px; }}
.adv-final h2 {{ font-family: var(--serif); font-size: 38px; font-weight: 700; margin-bottom: 14px; letter-spacing: -0.025em; max-width: 600px; margin-left: auto; margin-right: auto; line-height: 1.18; }}
.adv-final p {{ font-size: 17px; opacity: 0.95; margin-bottom: 28px; max-width: 480px; margin-left: auto; margin-right: auto; }}
.adv-final .adv-cta {{ background: #fff; color: var(--brand-dark) !important; box-shadow: 0 8px 24px rgba(0,0,0,0.20); }}
.adv-final .adv-cta:hover {{ box-shadow: 0 14px 32px rgba(0,0,0,0.30); }}
.adv-footer {{ max-width: 720px; margin: 0 auto; padding: 28px 20px; text-align: center; color: #999; font-size: 11.5px; line-height: 1.6; }}
.adv-sticky {{ position: fixed; bottom: 0; left: 0; right: 0; background: var(--brand-dark); color: #fff; padding: 14px 18px; display: none; justify-content: space-between; align-items: center; box-shadow: 0 -6px 24px rgba(0,0,0,0.24); z-index: 50; }}
.adv-sticky.show {{ display: flex; animation: stickyIn .3s ease; }}
@keyframes stickyIn {{ from {{ transform: translateY(100%); }} to {{ transform: translateY(0); }} }}
.adv-sticky-text {{ font-size: 13.5px; font-weight: 700; }}
.adv-sticky a {{ background: #fff; color: var(--brand-dark); padding: 11px 22px; border-radius: 8px; font-weight: 800; text-decoration: none; font-size: 13.5px; text-transform: uppercase; letter-spacing: 0.04em; }}
@media (max-width: 600px) {{
  .adv-hero h1 {{ font-size: 28px; }}
  .adv-hero .adv-sub {{ font-size: 16px; }}
  .adv-body {{ font-size: 16px; }}
  .adv-stats, .adv-how-grid {{ grid-template-columns: 1fr; }}
  .adv-final h2 {{ font-size: 26px; }}
}}
</style>
</head>
<body>

<div class="adv-topbar">{parsed.get('topbar_text','Безплатна доставка над 50 лв')}</div>

<header class="adv-header">
  <div class="adv-pub">{parsed.get('publication','Здравна Линия')}</div>
  <div class="adv-disclosure">Advertorial · {parsed.get('publish_date','')}</div>
</header>

<section class="adv-hero">
  <h1>{headline}</h1>
  <div class="adv-sub">{parsed.get('subheadline','')}</div>
  <div class="adv-stars"><span class="adv-stars-icons">★★★★★</span> {parsed.get('stars_text','')}</div>
  <img class="adv-hero-img" src="images/hero.jpg" alt="" onerror="this.style.display='none'">
  <a class="adv-cta" href="{cta_url}">{parsed.get('primary_cta_text','ПОРЪЧАЙ СЕГА →')}</a>
  <div class="adv-cta-meta">{parsed.get('cta_meta_text','')}</div>
</section>

<div class="adv-asseen">
  <div class="adv-asseen-label">{parsed.get('asseen_label','ПОКАЗАН В')}</div>
  <div class="adv-asseen-row">{asseen_html}</div>
</div>

<article class="adv-body">{body_html}</article>

<section class="adv-stats">{stats_html}</section>

<section class="adv-how">
  <h2>{parsed.get('how_title','Как работи за 3 стъпки')}</h2>
  <div class="adv-how-grid">{how_html}</div>
</section>

<section class="adv-testimonials">
  <h2>{parsed.get('testimonials_title','Какво казват клиентите')}</h2>
  <div class="adv-test-grid">{test_html}</div>
</section>

<section class="adv-guarantee">
  <div class="adv-guarantee-shield">🛡</div>
  <div class="adv-guarantee-title">{parsed.get('guarantee_title','60-ДНЕВНА ГАРАНЦИЯ')}</div>
  <div class="adv-guarantee-text">{parsed.get('guarantee_text','')}</div>
</section>

<div class="adv-author">
  <div class="adv-author-name">{parsed.get('author_name','')}</div>
  <div class="adv-author-title">{parsed.get('author_title','')}</div>
  <div class="adv-author-bio">{parsed.get('author_bio','')}</div>
</div>

<section class="adv-final">
  <h2>{parsed.get('final_headline','Не отлагай още един ден')}</h2>
  <p>{parsed.get('final_text','')}</p>
  <a class="adv-cta" href="{cta_url}">{parsed.get('primary_cta_text','ПОРЪЧАЙ СЕГА →')}</a>
</section>

<footer class="adv-footer">
  <p>{parsed.get('disclaimer','')}</p>
  <p style="margin-top:8px;">© {brief.get('brand','')} · {parsed.get('rights_text','Всички права запазени.')}</p>
</footer>

<div class="adv-sticky" id="advSticky">
  <span class="adv-sticky-text">{parsed.get('sticky_text','')}</span>
  <a href="{cta_url}">{parsed.get('sticky_cta_text','Поръчай')}</a>
</div>

<script>
window.addEventListener('scroll', () => {{
  document.getElementById('advSticky').classList.toggle('show', window.scrollY > 800);
}});
</script>
</body>
</html>'''
    return template


def adv_run_pipeline(job_id, brief):
    """Orchestrate the advertorial generation pipeline."""
    state = ADV_JOBS[job_id]
    state['started'] = time.time()
    job_dir = ADV_JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    images_dir = job_dir / 'images'
    images_dir.mkdir(exist_ok=True)

    try:
        # 1. Generate content via Claude/Gemini
        state.update(stage='content', stage_label='Claude пише статията…', progress_pct=10)
        _adv_save_state(job_id, state)
        raw = adv_generate_content(brief)
        (job_dir / 'raw_response.txt').write_text(raw, encoding='utf-8')

        # 2. Parse delimited content
        state.update(stage='parse', stage_label='Парсвам съдържанието…', progress_pct=35)
        _adv_save_state(job_id, state)
        parsed = adv_parse_delimited(raw)
        (job_dir / 'advertorial.json').write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding='utf-8')

        # 3. Generate images in parallel (Nano Banana Pro)
        state.update(stage='images', stage_label=f'Генерирам {len(parsed.get("images",[]))+1} снимки (Nano Banana)…', progress_pct=45, completed={'images': 0})
        _adv_save_state(job_id, state)

        all_images = []
        # Hero image
        if parsed.get('hero_image_prompt'):
            all_images.append({
                'id': 'hero',
                'prompt': parsed['hero_image_prompt'],
                'aspect': parsed.get('hero_image_aspect','16:9'),
                'shows_product': parsed.get('hero_shows_product', True),
            })
        all_images.extend(parsed.get('images', []))

        def gen_img(img, _retry=0):
            input_data = {
                'prompt': img['prompt'],
                'output_format': 'jpg',
                'aspect_ratio': img.get('aspect','16:9'),
            }
            if img.get('shows_product') and brief.get('product_image_url'):
                input_data['image_urls'] = [brief['product_image_url']]
            try:
                body = json.dumps({'model':'nano-banana-pro','input':input_data}).encode('utf-8')
                req = urllib.request.Request(
                    'https://api.kie.ai/api/v1/playground/createTask',
                    data=body,
                    headers={'Content-Type':'application/json','Authorization':f'Bearer {KIE_AI_KEY}','User-Agent':'BrandScraper/1.0'},
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    cresp = json.loads(r.read().decode('utf-8'))
                task_id = (cresp.get('data') or {}).get('taskId') or cresp.get('taskId')
                if not task_id: return (img['id'], None)
                for _ in range(60):
                    time.sleep(3)
                    try:
                        preq = urllib.request.Request(
                            f'https://api.kie.ai/api/v1/playground/recordInfo?taskId={task_id}',
                            headers={'Authorization':f'Bearer {KIE_AI_KEY}','User-Agent':'BrandScraper/1.0'})
                        with urllib.request.urlopen(preq, timeout=20) as r:
                            info = json.loads(r.read().decode('utf-8'))
                    except Exception: continue
                    d = info.get('data') or {}
                    st = (d.get('state') or '').lower()
                    if st in ('success','succeed','completed'):
                        rj = d.get('resultJson') or '{}'
                        rd = json.loads(rj) if isinstance(rj, str) else rj
                        urls = rd.get('resultUrls') or []
                        if urls:
                            # Download
                            local = images_dir / f'{img["id"]}.jpg'
                            try:
                                dreq = urllib.request.Request(urls[0], headers={'User-Agent':'Mozilla/5.0'})
                                with urllib.request.urlopen(dreq, timeout=60) as r:
                                    local.write_bytes(r.read())
                            except Exception as e:
                                print(f'[adv dl {img["id"]}] {e}', file=sys.stderr, flush=True)
                            return (img['id'], urls[0])
                    elif st in ('fail','failed','error'):
                        # Retry once with shorter prompt
                        if _retry == 0:
                            print(f'[adv img {img["id"]}] failed, retrying with simpler prompt', file=sys.stderr, flush=True)
                            simpler = {**img, 'prompt': img['prompt'].split('.')[0][:200]}
                            return gen_img(simpler, _retry=1)
                        return (img['id'], None)
            except Exception as e:
                print(f'[adv img {img["id"]}] {e}', file=sys.stderr, flush=True)
                if _retry == 0:
                    time.sleep(5)
                    return gen_img(img, _retry=1)
            return (img['id'], None)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(gen_img, im) for im in all_images]
            done = 0
            for fut in as_completed(futures):
                fut.result()
                done += 1
                state['completed']['images'] = done
                state['progress_pct'] = 45 + int(done / len(all_images) * 45)
                state['stage_label'] = f'Снимки… ({done}/{len(all_images)})'
                _adv_save_state(job_id, state)

        # 4. Render HTML
        state.update(stage='render', stage_label='Рендвам HTML…', progress_pct=95)
        _adv_save_state(job_id, state)
        html = adv_render_html(parsed, brief, job_id)
        (job_dir / 'index.html').write_text(html, encoding='utf-8')

        # Save final state
        state.update(stage='done', stage_label='Готово', progress_pct=100,
                     headline=parsed.get('headline','').replace('[HL]','').replace('[/HL]',''))
        _adv_save_state(job_id, state)

    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        state.update(stage='failed', error=str(e), progress_pct=0)
        _adv_save_state(job_id, state)


def _adv_save_state(job_id, state):
    p = ADV_JOBS_DIR / job_id
    p.mkdir(parents=True, exist_ok=True)
    (p / 'state.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    _history_sync(job_id, state, feature='advertorial')


def adv_create_job(brief, user_id=None):
    job_id = uuid.uuid4().hex[:12]
    state = {
        'job_id': job_id, 'user_id': user_id, 'brand': brief.get('brand',''),
        'stage': 'queued', 'stage_label': 'В опашка…',
        'progress_pct': 0, 'completed': {},
    }
    ADV_JOBS[job_id] = state
    _history_record_create(job_id, user_id, 'advertorial', brief)
    _adv_save_state(job_id, state)
    (ADV_JOBS_DIR / job_id / 'brief.json').write_text(json.dumps(brief, ensure_ascii=False), encoding='utf-8')
    threading.Thread(target=adv_run_pipeline, args=(job_id, brief), daemon=True).start()
    return job_id


def adv_get_job(job_id):
    state = ADV_JOBS.get(job_id)
    if not state:
        sf = ADV_JOBS_DIR / job_id / 'state.json'
        if sf.exists():
            state = json.loads(sf.read_text())
    if state and 'started' in state and state.get('stage') != 'done':
        state['elapsed_sec'] = int(time.time() - state['started'])
    return state


def extract_single_product(html, url):
    """Extract ONE product's data from a single product page."""
    soup = BeautifulSoup(html, 'html.parser')

    name = ''
    description = ''
    price = ''
    images = []

    # ── 1. JSON-LD Product schema (gold standard for Shopify/WooCommerce) ──
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            text = script.string or script.get_text() or ''
            data = json.loads(text)
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get('@graph', [data])
        for ld in items:
            if not isinstance(ld, dict):
                continue
            t = ld.get('@type', '')
            is_product = (
                t == 'Product' or t == 'Service'
                or (isinstance(t, list) and ('Product' in t or 'Service' in t))
            )
            if not is_product:
                continue
            name = name or (ld.get('name') or '').strip()
            description = description or (ld.get('description') or '').strip()
            # Price
            offers = ld.get('offers', {})
            if isinstance(offers, dict):
                price = price or str(offers.get('price', '') or offers.get('lowPrice', '') or '')
            elif isinstance(offers, list) and offers and isinstance(offers[0], dict):
                price = price or str(offers[0].get('price', ''))
            # Images
            img = ld.get('image', '')
            ld_imgs = []
            if isinstance(img, str):
                ld_imgs = [img]
            elif isinstance(img, list):
                for it in img:
                    if isinstance(it, str): ld_imgs.append(it)
                    elif isinstance(it, dict): ld_imgs.append(it.get('url') or it.get('contentUrl') or '')
            elif isinstance(img, dict):
                ld_imgs = [img.get('url') or img.get('contentUrl') or '']
            for im in ld_imgs:
                if im and im not in images:
                    images.append(im)

    # ── 2. H1 fallback for name ──
    if not name:
        h1 = soup.find('h1')
        if h1: name = re.sub(r'\s+', ' ', h1.get_text(strip=True))[:120]

    # ── 3. URL slug fallback ──
    if not name:
        slug = re.search(r'/products?/([^/?#]+)', url)
        if slug:
            try:
                slug_val = urllib.parse.unquote(slug.group(1))
                name = slug_val.replace('-', ' ').replace('_', ' ').strip().title()
            except Exception:
                pass

    # ── 4. Description fallback — first long product paragraph ──
    if not description:
        # Try Shopify-style description divs
        for sel in [
            {'class': re.compile(r'product[-_]description|product__description|product-single__description|rte', re.I)},
            None,  # any p
        ]:
            if sel:
                blocks = soup.find_all(['div', 'section'], attrs=sel)
            else:
                blocks = soup.find_all('p')
            for b in blocks:
                txt = re.sub(r'\s+', ' ', b.get_text(' ', strip=True))
                if 80 < len(txt) < 2000 and 'cookie' not in txt.lower() and 'количка' not in txt.lower():
                    description = txt
                    break
            if description:
                break
    if not description:
        description = (
            (soup.find('meta', property='og:description') or {}).get('content', '')
            or (soup.find('meta', attrs={'name': 'description'}) or {}).get('content', '')
            or ''
        )

    # ── 5. Price fallback — scan for currency patterns ──
    if not price:
        # Look for typical price patterns
        text = soup.get_text(' ', strip=True)
        m = re.search(r'(\d{1,4}[.,]\d{2})\s*(?:лв|BGN|€|EUR|\$)', text)
        if m:
            price = m.group(1).replace(',', '.')

    # ── 6. Images — gather from <img> tags too ──
    # Look for high-res product images in main content
    for tag in soup.find_all('img'):
        src = tag.get('src') or tag.get('data-src') or tag.get('data-original') or ''
        srcset = tag.get('srcset') or tag.get('data-srcset') or ''
        if srcset:
            # Pick the largest from srcset
            entries = [e.strip().split() for e in srcset.split(',') if e.strip()]
            if entries:
                # Last entry is usually largest
                src = entries[-1][0]
        if not src:
            continue
        full = urllib.parse.urljoin(url, src)
        if not full.startswith('http'):
            continue
        # Filter out icons, sprites, tracking
        lower = full.lower()
        skip = ['favicon', 'sprite', 'pixel', 'tracking', 'analytics', 'logo', 'icon-', '/icons/', '.svg']
        if any(s in lower for s in skip):
            continue
        # Prefer larger images — skip tiny ones based on size attrs
        try:
            w = int(tag.get('width') or 0)
            h = int(tag.get('height') or 0)
            if 0 < w < 100 and 0 < h < 100:
                continue
        except Exception:
            pass
        # Dedup
        if full not in images:
            images.append(full)

    # OG image as final fallback
    og_img = (soup.find('meta', property='og:image') or {}).get('content', '')
    if og_img and og_img not in images:
        images.insert(0, og_img)

    # Limit to 12 best
    images = images[:12]

    return {
        'name': name,
        'description': description,
        'price': price,
        'images': images,
        'url': url,
    }


# ═════════════════════════════════════════════
# ─── PixarADS — AI Ad Studio Pipeline ────────
# ═════════════════════════════════════════════
import threading
import time
import uuid
from pathlib import Path as _Path

PIXAR_JOBS = {}  # job_id → state dict
PIXAR_JOBS_DIR = _Path(__file__).parent.parent / '.tmp' / 'pixar_jobs'
PIXAR_JOBS_DIR.mkdir(parents=True, exist_ok=True)

from concurrent.futures import ThreadPoolExecutor, as_completed

STYLE_LOCKS = {
    'pixar_3d':    'Pixar-style 3D animation, glossy character textures, cinematic studio lighting, vivid colors, octane render quality.',
    'claymation':  'Stop-motion claymation aesthetic, plasticine textures, charming imperfections, warm lighting.',
    'anime':       'Anime style, cel-shaded characters, dynamic poses, vibrant colors, expressive eyes.',
    'ghibli':      'Studio Ghibli watercolor style, soft pastel palette, gentle hand-drawn feel, whimsical detail.',
    'voxel':       'Voxel 3D art, cubic block geometry, vibrant saturated palette, isometric lighting.',
    'comic':       'Comic book style, bold inking, halftone shading, dynamic action panels.',
    'flat_2d':     'Flat 2D vector animation, clean geometric shapes, limited color palette, modern editorial style.',
    'lowpoly':     'Low-poly 3D, faceted geometry, minimal materials, clean ambient lighting.',
    'pixel_art':   '16-bit pixel art animation, retro game aesthetic, limited palette, crisp pixels.',
    'watercolor':  'Loose watercolor painting style, paper texture, soft bleeds, hand-painted look.',
    'cinematic':   'Photorealistic cinematic live-action, shallow depth of field, color-graded warm tones.',
    'isometric':   'Isometric 3D illustration, 30-degree angle, clean shapes, soft shadows.',
    'paper_cut':   'Layered paper-cut craft animation, stacked construction paper, soft shadow gradients.',
    'cyberpunk':   'Cyberpunk neon aesthetic, magenta and cyan lighting, rain-slick surfaces, gritty future feel.',
    'minimal_3d':  'Minimalist 3D, white studio backdrop, soft shadows, clean geometric forms, brand-photo quality.',
}

CHAR_DESCRIPTIONS = {
    'pixar_skeleton':  'a friendly cartoon skeleton character with rounded bones and expressive eye sockets',
    'pixar_human':     'a stylized Pixar-style human character with warm expression',
    'animal':          'a charming anthropomorphic animal character',
    'mascot':          'a custom brand mascot character',
    'robot':           'a sleek friendly robot character',
    'monster':         'a cute fluffy monster character',
    'astronaut':       'an astronaut character in space suit',
    'wizard':          'a wise wizard character with flowing robes',
    'chef':            'a passionate chef character',
    'athlete':         'an energetic athlete character mid-action',
    'office_worker':   'a relatable office worker character',
    'elderly':         'a gentle elderly character with warm expression',
    'child':           'an excited child character',
    'fantasy_creature':'a whimsical fantasy creature',
    'no_character':    '',
}


def kie_elevenlabs_tts(text, voice='Rachel'):
    """Generate TTS via Kie AI's ElevenLabs multilingual v2. Returns mp3 URL."""
    if not KIE_AI_KEY:
        return None
    body = json.dumps({
        'model': 'elevenlabs/text-to-speech-multilingual-v2',
        'input': {
            'text': text[:2000],
            'voice': voice,
        }
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.kie.ai/api/v1/jobs/createTask',
        data=body,
        headers={
            'Content-Type':'application/json',
            'Authorization':f'Bearer {KIE_AI_KEY}',
            'User-Agent':'BrandScraper/1.0',
            'Accept':'application/json',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            cresp = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f'[tts submit] {e}', file=sys.stderr, flush=True)
        return None
    task_id = (cresp.get('data') or {}).get('taskId') or cresp.get('taskId')
    if not task_id:
        print(f'[tts] no taskId: {cresp}', file=sys.stderr, flush=True)
        return None
    print(f'[tts] task {task_id} submitted ({len(text)} chars)', file=sys.stderr, flush=True)
    for attempt in range(40):
        time.sleep(3)
        try:
            preq = urllib.request.Request(
                f'https://api.kie.ai/api/v1/jobs/recordInfo?taskId={task_id}',
                headers={'Authorization':f'Bearer {KIE_AI_KEY}','User-Agent':'BrandScraper/1.0'})
            with urllib.request.urlopen(preq, timeout=20) as r:
                info = json.loads(r.read().decode('utf-8'))
        except Exception as e:
            print(f'[tts poll] {e}', file=sys.stderr, flush=True); continue
        d = info.get('data') or {}
        st = (d.get('state') or '').lower()
        if st in ('success','succeed'):
            rj = d.get('resultJson') or '{}'
            rd = json.loads(rj) if isinstance(rj, str) else rj
            urls = rd.get('resultUrls') or []
            if urls:
                print(f'[tts] {task_id} SUCCESS', file=sys.stderr, flush=True)
                return urls[0]
        elif st in ('fail','failed','error'):
            print(f'[tts] {task_id} FAILED: {d.get("failMsg")}', file=sys.stderr, flush=True)
            return None
    return None


def kie_kling_video(image_url, prompt, duration='5', aspect='9:16'):
    """Generate image-to-video clip via Kling 2.6. Returns video URL or None."""
    if not KIE_AI_KEY:
        return None
    body = json.dumps({
        'model': 'kling-2.6/image-to-video',
        'input': {
            'prompt': prompt[:1500],
            'image_urls': [image_url],
            'sound': False,
            'duration': str(duration),
        }
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.kie.ai/api/v1/jobs/createTask',
        data=body,
        headers={
            'Content-Type':'application/json',
            'Authorization':f'Bearer {KIE_AI_KEY}',
            'User-Agent':'BrandScraper/1.0',
            'Accept':'application/json',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            cresp = json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print(f'[kling submit] {e}', file=sys.stderr, flush=True)
        return None
    task_id = (cresp.get('data') or {}).get('taskId') or cresp.get('taskId')
    if not task_id:
        print(f'[kling] no taskId in response: {cresp}', file=sys.stderr, flush=True)
        return None
    print(f'[kling] task {task_id} submitted', file=sys.stderr, flush=True)
    # Poll — Kling takes 1-3 min per clip
    for attempt in range(60):
        time.sleep(5)
        try:
            preq = urllib.request.Request(
                f'https://api.kie.ai/api/v1/jobs/recordInfo?taskId={task_id}',
                headers={
                    'Authorization':f'Bearer {KIE_AI_KEY}',
                    'User-Agent':'BrandScraper/1.0',
                    'Accept':'application/json',
                })
            with urllib.request.urlopen(preq, timeout=20) as r:
                info = json.loads(r.read().decode('utf-8'))
        except Exception as e:
            print(f'[kling poll attempt {attempt}] {e}', file=sys.stderr, flush=True)
            continue
        d = info.get('data') or {}
        st = (d.get('state') or '').lower()
        if attempt % 4 == 0:
            print(f'[kling] task {task_id} state={st}', file=sys.stderr, flush=True)
        if st in ('success','succeed'):
            rj = d.get('resultJson') or '{}'
            rd = json.loads(rj) if isinstance(rj, str) else rj
            urls = rd.get('resultUrls') or []
            if urls:
                print(f'[kling] task {task_id} SUCCESS: {urls[0][:80]}', file=sys.stderr, flush=True)
                return urls[0]
            return None
        elif st in ('fail','failed','error'):
            print(f'[kling] task {task_id} FAILED: {d.get("failMsg")}', file=sys.stderr, flush=True)
            return None
    print(f'[kling] task {task_id} TIMEOUT after 5 min', file=sys.stderr, flush=True)
    return None


def kie_claude_complete(prompt, max_tokens=4096):
    """Call Gemini 2.5 Pro via KIE.AI (Claude endpoint returns 403)."""
    if not KIE_AI_KEY:
        raise RuntimeError('KIE_AI_API_KEY not set')
    body = json.dumps({
        'model': 'gemini-2.5-pro',
        'messages': [{'role':'user','content':prompt}],
        'temperature': 0.7,
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.kie.ai/gemini-2.5-pro/v1/chat/completions',
        data=body,
        headers={
            'Content-Type':'application/json',
            'Authorization':f'Bearer {KIE_AI_KEY}',
            'User-Agent':'BrandScraper/1.0',
            'Accept':'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode('utf-8'))
    return data.get('choices',[{}])[0].get('message',{}).get('content','')


def pixar_generate_script(payload):
    brand = payload.get('brand','')
    product = payload.get('product','')
    desc = payload.get('brand_description','')
    audience = payload.get('audience','')
    angle = payload.get('angle','')
    tone = payload.get('tone','wholesome')
    duration = payload.get('duration_sec', 30)
    scenes = payload.get('scenes','auto')
    n_scenes = max(3, round(duration/5)) if scenes == 'auto' else int(scenes)
    prompt = (
        f"Напиши voiceover скрипт за {duration} секунди реклама на български език.\n\n"
        f"БРАНД: {brand}\nПРОДУКТ: {product}\nОПИСАНИЕ: {desc}\n"
        f"АУДИТОРИЯ: {audience}\nАНГЪЛ/USP: {angle}\nТОН: {tone}\n"
        f"СЦЕНИ: {n_scenes}\n\n"
        f"Изисквания:\n"
        f"- Точно {n_scenes} реплики, по една на сцена\n"
        f"- Всяка с timestamp в началото (0:00, 0:05, и т.н.)\n"
        f"- На последния ред включи името на бранда\n"
        f"- Кратки, разговорни изречения\n"
        f"- Без емоджита\n"
        f"- Без 'спестявайте сега', 'оферта' и подобни клишета\n\n"
        f"Формат:\n"
        f"0:00 Текст на първа сцена.\n"
        f"0:05 Текст на втора сцена.\n"
        f"..."
    )
    return kie_claude_complete(prompt, 1500)


def _pixar_job_state_path(job_id):
    return PIXAR_JOBS_DIR / job_id / 'state.json'


def _pixar_save_state(job_id, state):
    p = PIXAR_JOBS_DIR / job_id
    p.mkdir(parents=True, exist_ok=True)
    (p / 'state.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    _history_sync(job_id, state, feature='pixar')


def _pixar_run_pipeline(job_id, brief):
    """Run the full ad pipeline. Updates PIXAR_JOBS[job_id] in place."""
    state = PIXAR_JOBS[job_id]
    state['started'] = time.time()
    job_dir = PIXAR_JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / 'keyframes').mkdir(exist_ok=True)
    (job_dir / 'clips').mkdir(exist_ok=True)
    (job_dir / 'audio').mkdir(exist_ok=True)

    try:
        # Parse VO script into shots
        script = brief.get('voiceover_script','')
        lines = [l.strip() for l in script.split('\n') if l.strip() and re.match(r'^\d+:\d+', l.strip())]
        n_shots = len(lines) or 6

        # STAGE 1: scene plan (via Claude)
        state.update(stage='scene_plan', stage_label='Планирам сцени с Claude…', progress_pct=5, n_shots=n_shots)
        _pixar_save_state(job_id, state)

        style_lock = STYLE_LOCKS.get(brief.get('style','pixar_3d'), STYLE_LOCKS['pixar_3d'])
        char_desc = CHAR_DESCRIPTIONS.get(brief.get('character','pixar_skeleton'), '')

        scene_plan_prompt = (
            f"Plan {n_shots} scenes for an ad. Style: {style_lock}\nCharacter: {char_desc or 'no main character'}\n"
            f"Setting hint: {brief.get('setting','')}\nTone: {brief.get('tone','')}\nProduct: {brief.get('product_name','')}\n"
            f"VO lines (one per scene):\n" + '\n'.join(lines) +
            "\n\nReturn ONLY a JSON array of objects with: location, framing, subject, action, props (array), mood, camera_motion, product_visible (bool). "
            f"Variety required: never repeat location/framing/subject in consecutive scenes. product_visible=true only when VO mentions the brand."
        )
        try:
            plan_text = kie_claude_complete(scene_plan_prompt, 3000)
            plan_text = re.sub(r'```json\s*|\s*```', '', plan_text).strip()
            m = re.search(r'\[[\s\S]*\]', plan_text)
            scene_plan = json.loads(m.group(0)) if m else []
        except Exception as e:
            print(f'[pixar plan] {e}', file=sys.stderr)
            scene_plan = [{'location':brief.get('setting','minimalist studio'),'framing':'medium shot','action':'showcase','props':[],'mood':brief.get('tone','calm'),'camera_motion':'slow push-in','product_visible':i==n_shots-1} for i in range(n_shots)]

        state['scene_plan'] = scene_plan
        _pixar_save_state(job_id, state)

        # STAGE 2: keyframes
        state.update(stage='keyframes', stage_label='Генерирам keyframes…', progress_pct=15, completed={'keyframes':0,'clips':0})
        _pixar_save_state(job_id, state)

        anchor_url = None
        keyframes = []
        for i, scene in enumerate(scene_plan):
            kf_prompt = f"{style_lock} {scene.get('framing','medium shot')} of {char_desc or 'product'}. Location: {scene.get('location')}. Action: {scene.get('action')}. Mood: {scene.get('mood')}. No text overlay, no watermark."
            refs = []
            if anchor_url: refs.append(anchor_url)
            if scene.get('product_visible') and brief.get('product_reference_url'):
                refs.append(brief['product_reference_url'])
            input_data = {
                'prompt': kf_prompt,
                'output_format': 'jpg',
                'aspect_ratio': brief.get('aspect_ratio','9:16'),
            }
            if refs: input_data['image_input'] = refs
            try:
                body = json.dumps({'model':'nano-banana-pro','input':input_data}).encode('utf-8')
                req = urllib.request.Request('https://api.kie.ai/api/v1/playground/createTask', data=body,
                    headers={'Content-Type':'application/json','Authorization':f'Bearer {KIE_AI_KEY}'}, method='POST')
                with urllib.request.urlopen(req, timeout=30) as r:
                    cresp = json.loads(r.read().decode('utf-8'))
                task_id = (cresp.get('data') or {}).get('taskId') or cresp.get('taskId')
                # Poll
                url = None
                for _ in range(60):
                    time.sleep(3)
                    preq = urllib.request.Request(f'https://api.kie.ai/api/v1/playground/recordInfo?taskId={task_id}',
                        headers={'Authorization':f'Bearer {KIE_AI_KEY}'})
                    with urllib.request.urlopen(preq, timeout=20) as r:
                        info = json.loads(r.read().decode('utf-8'))
                    d = info.get('data') or {}
                    st = (d.get('state') or '').lower()
                    if st in ('success','succeed'):
                        rj = d.get('resultJson') or '{}'
                        rd = json.loads(rj) if isinstance(rj, str) else rj
                        urls = rd.get('resultUrls') or []
                        if urls: url = urls[0]; break
                    elif st in ('fail','failed','error'):
                        break
                if not url: raise RuntimeError(f'No URL for shot {i+1}')
                if i == 0: anchor_url = url
                # Save URL
                (job_dir / 'keyframes' / f'shot_{i+1:02d}.url.txt').write_text(url)
                keyframes.append({'shot': i+1, 'url': url})
                state['completed']['keyframes'] = i+1
                state['progress_pct'] = 15 + int((i+1)/n_shots * 30)
                _pixar_save_state(job_id, state)
            except Exception as e:
                print(f'[pixar keyframe {i+1}] {e}', file=sys.stderr)

        # Save keyframes to local disk (TempFile URLs expire) and serve via backend
        for kf in keyframes:
            try:
                local = job_dir / 'keyframes' / f'shot_{kf["shot"]:02d}.jpg'
                if not local.exists():
                    dreq = urllib.request.Request(kf['url'], headers={'User-Agent':'Mozilla/5.0'})
                    with urllib.request.urlopen(dreq, timeout=30) as r:
                        local.write_bytes(r.read())
            except Exception as e:
                print(f'[pixar dl keyframe] {e}', file=sys.stderr, flush=True)
        state['keyframes'] = [f'/pixar/jobs/{job_id}/keyframes/{kf["shot"]}.jpg' for kf in keyframes]
        state['scene_plan'] = scene_plan
        state['brief_script'] = brief.get('voiceover_script','')
        _pixar_save_state(job_id, state)

        # Approval gate
        if brief.get('approval_gate'):
            state.update(stage='awaiting_approval', stage_label='Изчаквам одобрение на keyframes…', progress_pct=45)
            _pixar_save_state(job_id, state)
            for _ in range(1200):  # wait up to 1 hour
                time.sleep(3)
                if state.get('approved'): break
                if state.get('cancelled'): return

        # STAGE 3: Kling 2.6 image-to-video clips (parallel, 4 workers)
        state.update(stage='clips', stage_label='Генерирам анимирани clips с Kling 2.6…', progress_pct=50, completed={'keyframes': len(keyframes), 'clips': 0})
        _pixar_save_state(job_id, state)

        clips = [None] * len(keyframes)
        aspect = brief.get('aspect_ratio','9:16')

        def gen_one_clip(idx_kf):
            i, kf = idx_kf
            scene = scene_plan[i] if i < len(scene_plan) else {}
            motion_prompt = f"{scene.get('camera_motion','slow push-in')}. {scene.get('action','character moves naturally')}. Continuous fluid motion, cinematic. {STYLE_LOCKS.get(brief.get('style','pixar_3d'), '')[:200]}"
            video_url = kie_kling_video(kf['url'], motion_prompt, duration='5', aspect=aspect)
            if video_url:
                # Download locally
                local_path = job_dir / 'clips' / f'clip_{kf["shot"]:02d}.mp4'
                try:
                    dreq = urllib.request.Request(video_url, headers={'User-Agent':'Mozilla/5.0'})
                    with urllib.request.urlopen(dreq, timeout=120) as r:
                        local_path.write_bytes(r.read())
                    print(f'[pixar] clip {kf["shot"]} saved ({local_path.stat().st_size} bytes)', file=sys.stderr, flush=True)
                except Exception as e:
                    print(f'[pixar dl clip {kf["shot"]}] {e}', file=sys.stderr, flush=True)
            return (i, video_url)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(gen_one_clip, (i, kf)) for i, kf in enumerate(keyframes)]
            done_count = 0
            for fut in as_completed(futures):
                i, url = fut.result()
                clips[i] = url
                done_count += 1
                state['completed']['clips'] = done_count
                state['progress_pct'] = 50 + int(done_count / len(keyframes) * 40)
                state['stage_label'] = f'Kling clips… ({done_count}/{len(keyframes)})'
                _pixar_save_state(job_id, state)

        # Save clip URLs (use local served URLs)
        state['clips'] = []
        for i, url in enumerate(clips):
            shot_n = keyframes[i]['shot']
            local = job_dir / 'clips' / f'clip_{shot_n:02d}.mp4'
            if local.exists():
                state['clips'].append(f'/pixar/jobs/{job_id}/clips/{shot_n}.mp4')
            elif url:
                state['clips'].append(url)
            else:
                state['clips'].append(None)
        _pixar_save_state(job_id, state)

        # STAGE 4: Voiceover (ElevenLabs multilingual via Kie AI)
        state.update(stage='voiceover', stage_label='Генерирам voiceover (ElevenLabs)…', progress_pct=92)
        _pixar_save_state(job_id, state)

        audio_dir = job_dir / 'audio'
        audio_dir.mkdir(exist_ok=True)
        voice_name = brief.get('voice', 'Rachel')
        audio_urls = [None] * len(lines)

        def gen_one_audio(idx_line):
            i, line = idx_line
            text = re.sub(r'^\d+:\d+\s*', '', line).strip()
            if not text: return (i, None)
            audio_url = kie_elevenlabs_tts(text, voice=voice_name)
            if audio_url:
                local = audio_dir / f'vo_{i+1:02d}.mp3'
                try:
                    dreq = urllib.request.Request(audio_url, headers={'User-Agent':'Mozilla/5.0'})
                    with urllib.request.urlopen(dreq, timeout=60) as r:
                        local.write_bytes(r.read())
                    print(f'[pixar] voiceover {i+1} saved ({local.stat().st_size} bytes)', file=sys.stderr, flush=True)
                except Exception as e:
                    print(f'[pixar dl vo {i+1}] {e}', file=sys.stderr, flush=True)
            return (i, audio_url)

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(gen_one_audio, (i, line)) for i, line in enumerate(lines)]
            for fut in as_completed(futures):
                i, url = fut.result()
                audio_urls[i] = url

        state['audio'] = []
        for i, url in enumerate(audio_urls):
            local = audio_dir / f'vo_{i+1:02d}.mp3'
            if local.exists():
                state['audio'].append(f'/pixar/jobs/{job_id}/audio/{i+1}.mp3')
            elif url:
                state['audio'].append(url)
            else:
                state['audio'].append(None)

        state.update(stage='done', stage_label='Готово', progress_pct=100)
        _pixar_save_state(job_id, state)

    except Exception as e:
        state.update(stage='failed', error=str(e), progress_pct=0)
        _pixar_save_state(job_id, state)
        print(f'[pixar pipeline] {e}', file=sys.stderr)


def pixar_create_job(brief, user_id=None):
    job_id = uuid.uuid4().hex[:12]
    state = {
        'job_id': job_id,
        'user_id': user_id,
        'brand': brief.get('brand',''),
        'stage': 'queued',
        'stage_label': 'В опашка…',
        'progress_pct': 0,
        'elapsed_sec': 0,
        'n_shots': 0,
        'completed': {},
        'keyframes': [],
        'approved': False,
        'cancelled': False,
    }
    PIXAR_JOBS[job_id] = state
    _history_record_create(job_id, user_id, 'pixar', brief)
    _pixar_save_state(job_id, state)
    # Persist brief
    (PIXAR_JOBS_DIR / job_id / 'brief.json').write_text(json.dumps(brief, ensure_ascii=False), encoding='utf-8')
    # Start pipeline in background
    threading.Thread(target=_pixar_run_pipeline, args=(job_id, brief), daemon=True).start()
    return job_id


def pixar_get_job(job_id):
    state = PIXAR_JOBS.get(job_id)
    if not state: return None
    if 'started' in state and state['stage'] != 'done':
        state['elapsed_sec'] = int(time.time() - state['started'])
        # Rough ETA: 5 min total for a typical job
        state['eta_sec'] = max(0, 300 - state['elapsed_sec'])
    return state


def generate_static_via_nano_banana(prompt, product_image='', product_images=None, inspiration_image='', ratio='1:1'):
    """Use KIE AI's Nano Banana Pro to generate a static creative."""
    if not KIE_AI_KEY:
        return {'error': 'KIE_AI_API_KEY not set'}

    # 1. createTask
    input_data = {
        'prompt': prompt,
        'aspect_ratio': ratio,
        'output_format': 'png',
    }
    # Reference images — INSPIRATION FIRST (dominates layout/composition), then product
    ref_images = []
    if inspiration_image and inspiration_image.startswith('http'):
        ref_images.append(inspiration_image)  # FIRST → layout/style anchor
    if product_image and product_image.startswith('http') and product_image not in ref_images:
        ref_images.append(product_image)      # SECOND → product identity
    if product_images and isinstance(product_images, list):
        for img in product_images:
            if img and img.startswith('http') and img not in ref_images:
                ref_images.append(img)
    if ref_images:
        input_data['image_urls'] = ref_images[:4]

    req_body = json.dumps({
        'model': 'nano-banana-pro',
        'input': input_data,
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.kie.ai/api/v1/jobs/createTask',
        data=req_body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {KIE_AI_KEY}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            create_resp = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return {'error': f'createTask failed: {e}'}

    task_id = (create_resp.get('data') or {}).get('taskId') or create_resp.get('taskId')
    if not task_id:
        return {'error': f'No taskId in response: {create_resp}'}

    # 2. Poll recordInfo
    import time
    poll_url = f'https://api.kie.ai/api/v1/jobs/recordInfo?taskId={task_id}'
    for attempt in range(60):  # up to ~3 minutes
        time.sleep(3)
        try:
            poll_req = urllib.request.Request(
                poll_url,
                headers={'Authorization': f'Bearer {KIE_AI_KEY}'},
            )
            with urllib.request.urlopen(poll_req, timeout=20) as r:
                info = json.loads(r.read().decode('utf-8'))
        except Exception as e:
            print(f'[poll] {e}', file=sys.stderr)
            continue

        d = info.get('data') or {}
        state = (d.get('state') or '').lower()
        if state == 'success':
            result_json = d.get('resultJson') or '{}'
            try:
                result = json.loads(result_json) if isinstance(result_json, str) else result_json
            except Exception:
                result = {}
            urls = result.get('resultUrls') or result.get('urls') or []
            if urls:
                return {'image_url': urls[0], 'task_id': task_id}
            return {'error': 'success but no URLs', 'raw': result}
        elif state in ('fail', 'failed', 'error'):
            return {'error': f'Task failed: {d.get("failMsg") or info}'}

    return {'error': 'Timeout after 3 minutes'}


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Max-Age', '86400')

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        # ── Auth routes ──
        if parsed.path == '/auth/signup':
            length = int(self.headers.get('Content-Length', 0))
            try:
                payload = json.loads(self.rfile.read(length).decode('utf-8'))
                result = signup_user(payload.get('email',''), payload.get('password',''), payload.get('name',''))
                self._reply(400 if 'error' in result else 200, result)
            except Exception as e:
                self._reply(500, {'error': str(e)})
            return

        if parsed.path == '/auth/login':
            length = int(self.headers.get('Content-Length', 0))
            try:
                payload = json.loads(self.rfile.read(length).decode('utf-8'))
                result = login_user(payload.get('email',''), payload.get('password',''))
                self._reply(401 if 'error' in result else 200, result)
            except Exception as e:
                self._reply(500, {'error': str(e)})
            return

        # ── Advertorial routes ──
        if parsed.path == '/advertorial/angles':
            length = int(self.headers.get('Content-Length', 0))
            try:
                payload = json.loads(self.rfile.read(length).decode('utf-8'))
                angles = adv_generate_angles(
                    payload.get('brand',''), payload.get('product_name',''),
                    payload.get('product_desc',''), payload.get('audience',''),
                    payload.get('brand_doc',''),
                )
                self._reply(200, {'angles': angles})
            except Exception as e:
                import traceback; traceback.print_exc(file=sys.stderr)
                self._reply(500, {'error': str(e)})
            return

        if parsed.path == '/advertorial/jobs':
            length = int(self.headers.get('Content-Length', 0))
            try:
                brief = json.loads(self.rfile.read(length).decode('utf-8'))
                uid = auth_user_id(self.headers)
                job_id = adv_create_job(brief, user_id=uid)
                self._reply(200, {'job_id': job_id})
            except Exception as e:
                self._reply(500, {'error': str(e)})
            return

        # ── PixarADS routes ──
        if parsed.path == '/pixar/script':
            length = int(self.headers.get('Content-Length', 0))
            try:
                payload = json.loads(self.rfile.read(length).decode('utf-8'))
                text = pixar_generate_script(payload)
                self._reply(200, {'script': text})
            except Exception as e:
                self._reply(500, {'error': str(e)})
            return

        if parsed.path == '/pixar/jobs':
            length = int(self.headers.get('Content-Length', 0))
            try:
                brief = json.loads(self.rfile.read(length).decode('utf-8'))
                uid = auth_user_id(self.headers)
                job_id = pixar_create_job(brief, user_id=uid)
                self._reply(200, {'job_id': job_id})
            except Exception as e:
                self._reply(500, {'error': str(e)})
            return

        m = re.match(r'/pixar/jobs/([a-f0-9]+)/approve', parsed.path)
        if m:
            job_id = m.group(1)
            if job_id in PIXAR_JOBS:
                PIXAR_JOBS[job_id]['approved'] = True
                self._reply(200, {'ok': True})
            else:
                self._reply(404, {'error':'not found'})
            return

        if parsed.path == '/generate-static':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8') if length else '{}'
            try:
                req_data = json.loads(body)
            except Exception:
                self._reply(400, {'error': 'invalid JSON'})
                return
            result = generate_static_via_nano_banana(
                prompt=req_data.get('prompt', ''),
                product_image=req_data.get('product_image', ''),
                product_images=req_data.get('product_images', []),
                inspiration_image=req_data.get('inspiration_image', ''),
                ratio=req_data.get('ratio', '1:1'),
            )
            self._reply(200 if 'image_url' in result else 500, result)
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # ── Frontend: serve layout_preview.html as index ──
        if parsed.path in ('/', '/index.html', '/index'):
            html_path = Path(__file__).parent.parent / 'layout_preview.html'
            if html_path.exists():
                data = html_path.read_bytes()
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                try: self.wfile.write(data)
                except BrokenPipeError: pass
                return
            self._reply(404, {'error':'no frontend'}); return

        # Favicon — return empty 200 to avoid noisy 404s
        if parsed.path == '/favicon.ico':
            self.send_response(204); self._cors(); self.end_headers(); return

        # ── Auth ──
        if parsed.path == '/auth/me':
            uid = auth_user_id(self.headers)
            u = get_user_by_id(uid)
            if u: self._reply(200, {'user': u})
            else: self._reply(401, {'error': 'not authenticated'})
            return

        # ── PixarADS — serve local files ──
        def _send_file(path, ctype):
            try:
                data = path.read_bytes()
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.end_headers()
                self.wfile.write(data)
            except BrokenPipeError:
                pass

        # ── Ad Templates ──
        if parsed.path == '/ad-templates/list':
            qs = urllib.parse.parse_qs(parsed.query)
            cat = qs.get('category', [None])[0]
            self._reply(200, list_ad_templates(cat))
            return

        m = re.match(r'/ad-templates/([\w\-]+)/(.+)', parsed.path)
        if m:
            cat, fname = m.group(1), urllib.parse.unquote(m.group(2))
            local = AD_TEMPLATES_DIR / cat / fname
            if local.exists():
                ext = local.suffix.lower()
                ctype = {'.webp':'image/webp','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg'}.get(ext, 'image/jpeg')
                _send_file(local, ctype); return
            self._reply(404, {'error':'not found'}); return

        # ── Advertorial GET routes ──
        if parsed.path == '/advertorial/jobs':
            uid = auth_user_id(self.headers)
            jobs = []
            for jd in sorted(ADV_JOBS_DIR.iterdir(), key=lambda p: -p.stat().st_mtime):
                if not jd.is_dir(): continue
                sf = jd / 'state.json'
                if not sf.exists(): continue
                try:
                    st = json.loads(sf.read_text())
                    if st.get('stage') != 'done': continue
                    job_uid = st.get('user_id')
                    if uid:
                        if job_uid and job_uid != uid: continue
                    else:
                        if job_uid: continue
                    jobs.append({
                        'job_id': st.get('job_id'), 'brand': st.get('brand', ''),
                        'headline': st.get('headline', ''),
                        'created': jd.stat().st_mtime,
                    })
                except Exception: pass
            self._reply(200, {'jobs': jobs[:20]}); return

        m = re.match(r'/advertorial/jobs/([a-f0-9]+)/preview', parsed.path)
        if m:
            job_id = m.group(1)
            local = ADV_JOBS_DIR / job_id / 'index.html'
            if local.exists():
                # Rewrite image src to absolute URL paths
                html = local.read_text(encoding='utf-8')
                html = html.replace('src="images/', f'src="/advertorial/jobs/{job_id}/images/')
                body = html.encode('utf-8')
                self.send_response(200); self._cors()
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body); return
            self._reply(404, {'error':'not found'}); return

        m = re.match(r'/advertorial/jobs/([a-f0-9]+)/images/([\w\.]+)', parsed.path)
        if m:
            job_id, fname = m.group(1), m.group(2)
            local = ADV_JOBS_DIR / job_id / 'images' / fname
            if local.exists():
                _send_file(local, 'image/jpeg'); return
            self._reply(404, {'error':'not found'}); return

        m = re.match(r'/advertorial/jobs/([a-f0-9]+)$', parsed.path)
        if m:
            job_id = m.group(1)
            state = adv_get_job(job_id)
            if state: self._reply(200, state)
            else: self._reply(404, {'error':'not found'})
            return

        m = re.match(r'/pixar/jobs/([a-f0-9]+)/keyframes/(\d+)\.jpg', parsed.path)
        if m:
            job_id, idx = m.group(1), int(m.group(2))
            local = PIXAR_JOBS_DIR / job_id / 'keyframes' / f'shot_{idx:02d}.jpg'
            if local.exists():
                _send_file(local, 'image/jpeg'); return
            # Fallback: redirect to remote URL if we have one
            url_file = PIXAR_JOBS_DIR / job_id / 'keyframes' / f'shot_{idx:02d}.url.txt'
            if url_file.exists():
                self.send_response(302); self._cors()
                self.send_header('Location', url_file.read_text().strip())
                self.end_headers(); return
            self._reply(404, {'error':'not found'}); return

        m = re.match(r'/pixar/jobs/([a-f0-9]+)/clips/(\d+)\.mp4', parsed.path)
        if m:
            job_id, idx = m.group(1), int(m.group(2))
            local = PIXAR_JOBS_DIR / job_id / 'clips' / f'clip_{idx:02d}.mp4'
            if local.exists():
                _send_file(local, 'video/mp4'); return
            self._reply(404, {'error':'not found'}); return

        m = re.match(r'/pixar/jobs/([a-f0-9]+)/audio/(\d+)\.mp3', parsed.path)
        if m:
            job_id, idx = m.group(1), int(m.group(2))
            local = PIXAR_JOBS_DIR / job_id / 'audio' / f'vo_{idx:02d}.mp3'
            if local.exists():
                _send_file(local, 'audio/mpeg'); return
            self._reply(404, {'error':'not found'}); return

        m = re.match(r'/pixar/jobs/([a-f0-9]+)/preview', parsed.path)
        if m:
            job_id = m.group(1)
            local = PIXAR_JOBS_DIR / job_id / 'keyframes' / 'shot_01.jpg'
            if local.exists():
                _send_file(local, 'image/jpeg'); return
            url_file = PIXAR_JOBS_DIR / job_id / 'keyframes' / 'shot_01.url.txt'
            if url_file.exists():
                self.send_response(302); self._cors()
                self.send_header('Location', url_file.read_text().strip())
                self.end_headers(); return
            self._reply(404, {'error':'no preview'}); return

        # List all jobs (history)
        if parsed.path == '/pixar/jobs':
            uid = auth_user_id(self.headers)
            jobs = []
            for jd in sorted(PIXAR_JOBS_DIR.iterdir(), key=lambda p: -p.stat().st_mtime):
                if not jd.is_dir(): continue
                sf = jd / 'state.json'
                if not sf.exists(): continue
                try:
                    st = json.loads(sf.read_text())
                    if st.get('stage') != 'done': continue
                    # Logged-in user sees their own + unowned legacy. Anonymous sees only unowned.
                    job_uid = st.get('user_id')
                    if uid:
                        if job_uid and job_uid != uid: continue
                    else:
                        if job_uid: continue
                    clips_dir = jd / 'clips'
                    clip_files = sorted(clips_dir.glob('*.mp4')) if clips_dir.exists() else []
                    if not clip_files: continue
                    audio_dir = jd / 'audio'
                    audio_files = sorted(audio_dir.glob('*.mp3')) if audio_dir.exists() else []
                    jobs.append({
                        'job_id': st.get('job_id'),
                        'brand': st.get('brand', ''),
                        'created': jd.stat().st_mtime,
                        'n_scenes': len(clip_files),
                        'clips': [f'/pixar/jobs/{st.get("job_id")}/clips/{i}.mp4' for i in range(1, len(clip_files)+1)],
                        'frames': [f'/pixar/jobs/{st.get("job_id")}/keyframes/{i}.jpg' for i in range(1, len(clip_files)+1)],
                        'audio':  [f'/pixar/jobs/{st.get("job_id")}/audio/{i}.mp3' for i in range(1, len(audio_files)+1)] if audio_files else [],
                        'captions': [(s.get('action','Сцена')[:80] if isinstance(s, dict) else 'Сцена') for s in (st.get('scene_plan') or [])],
                        'script': st.get('brief_script',''),
                    })
                except Exception as e:
                    print(f'[list jobs] {e}', file=sys.stderr)
            self._reply(200, {'jobs': jobs[:20]})
            return

        m = re.match(r'/pixar/jobs/([a-f0-9]+)$', parsed.path)
        if m:
            job_id = m.group(1)
            state = pixar_get_job(job_id)
            if not state:
                # Try loading from disk
                sf = PIXAR_JOBS_DIR / job_id / 'state.json'
                if sf.exists():
                    state = json.loads(sf.read_text())
            if state:
                self._reply(200, state)
            else:
                self._reply(404, {'error':'not found'})
            return

        # ── Cross-feature job history (DB-backed) ──
        if parsed.path == '/api/history':
            uid = auth_user_id(self.headers)
            if not uid:
                self._reply(401, {'error': 'auth required'}); return
            if not _db.is_enabled():
                self._reply(503, {'error': 'history requires DATABASE_URL', 'jobs': []}); return
            params = urllib.parse.parse_qs(parsed.query)
            feature = (params.get('feature', [''])[0] or '').strip() or None
            try:
                limit = max(1, min(200, int(params.get('limit', ['50'])[0])))
            except Exception:
                limit = 50
            try:
                jobs = _db.history_list_by_user(uid, feature=feature, limit=limit)
                self._reply(200, {'jobs': jobs})
            except Exception as e:
                print(f'[api/history] {e}', file=sys.stderr)
                self._reply(500, {'error': 'history query failed'})
            return

        if parsed.path == '/scrape-product':
            params = urllib.parse.parse_qs(parsed.query)
            url = params.get('url', [''])[0].strip()
            if not url:
                self._reply(400, {'error': 'url required'})
                return
            try:
                if not url.startswith('http'):
                    url = 'https://' + url
                html = fetch_page(url)
                data = extract_single_product(html, url)
                self._reply(200, data)
            except urllib.error.HTTPError as e:
                print(f'[scrape-product] HTTP {e.code} for {url}', file=sys.stderr)
                msg = f'Страницата не съществува (HTTP {e.code}). Провери дали URL-ът е пълен и правилен.' if e.code == 404 \
                    else f'Сайтът върна HTTP {e.code}'
                self._reply(400, {'error': msg})
            except Exception as e:
                print(f'[scrape-product] {e}', file=sys.stderr)
                self._reply(500, {'error': str(e)})
            return

        if parsed.path != '/scrape':
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        url = params.get('url', [''])[0].strip()
        if not url:
            self._reply(400, {'error': 'url query param required'})
            return

        try:
            data = deep_scrape(url)
            self._reply(200, data)
        except Exception as e:
            print(f'[scrape error] {e}', file=sys.stderr)
            self._reply(500, {'error': str(e)})

    def _reply(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main():
    # PORT from env (Render/Railway/Fly inject this) or 8766 fallback
    port = int(os.environ.get('PORT', '8766'))
    # Bind to all interfaces in production, localhost otherwise
    host = '0.0.0.0' if os.environ.get('PORT') else 'localhost'
    print(f'🌐 AI Brand Scale Server → http://{host}:{port}', flush=True)
    print(f'   Threading: ✓  BeautifulSoup: ✓', flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == '__main__':
    main()
