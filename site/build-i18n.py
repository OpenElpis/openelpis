#!/usr/bin/env python3
"""
Build static pages for OpenElpis from index.html (the English source) + translations.json.

Produces:
  1. Per-language homepages /tr/ /es/ /de/ /fr/ /it/ /ru/ /nl/  (hreflang-linked)
  2. Founder name-profile pages (EN + TR only) for SEO:
       /ali-tajbakhsh/        /tr/ali-tajbakhsh/
       /mohammadali-tajbakhsh/ /tr/mohammadali-tajbakhsh/
       /alan-tajbakhsh/       /tr/alan-tajbakhsh/
  3. sitemap.xml covering everything (homepages + founder + profiles, with hreflang)

Why per-language URLs: client-side JS translation keeps every language on one URL, so
search engines only index one. Baking each into its own crawlable URL with hreflang is the
SEO-correct way. The name-profile pages target each spelling of the founder's name.

Run from the site/ directory:  python3 build-i18n.py
"""
import re, json, html, os

LANGS = ["tr", "es", "de", "fr", "it", "ru", "nl"]          # en is the source (/)
ALL = ["en"] + LANGS
URL = {l: ("/" if l == "en" else f"/{l}/") for l in ALL}
LOCALE = {"en": "en_US", "tr": "tr_TR", "es": "es_ES", "de": "de_DE",
          "fr": "fr_FR", "it": "it_IT", "ru": "ru_RU", "nl": "nl_NL"}
BASE = "https://openelpis.com"
LASTMOD = "2026-06-11"

tr_all = json.load(open("translations.json", encoding="utf-8"))
src = open("index.html", encoding="utf-8").read()

I18N_RE = re.compile(r'(<(\w+)\b[^>]*\bdata-i18n="([^"]+)"[^>]*>)(.*?)(</\2>)', re.S)
PH_RE = re.compile(r'(data-i18n-ph="([^"]+)"[^>]*\splaceholder=")([^"]*)(")')

def esc_text(v): return html.escape(v, quote=False)
def esc_attr(v): return html.escape(v, quote=True)

# ───────────────────────── 1. per-language homepages ─────────────────────────
def localize(htmltext, lang):
    tr = tr_all[lang]
    def repl(m):
        opentag, _tag, key, _inner, close = m.groups()
        if key not in tr: return m.group(0)
        val = tr[key]
        return opentag + (val if key.endswith("_html") else esc_text(val)) + close
    out = I18N_RE.sub(repl, htmltext)
    def repl_ph(m):
        pre, key, _old, q = m.groups()
        return pre + (esc_attr(tr[key]) if key in tr else _old) + q
    out = PH_RE.sub(repl_ph, out)
    out = out.replace('<html lang="en">', f'<html lang="{lang}">', 1)
    out = re.sub(r'<title>.*?</title>', f'<title>{esc_text(tr["page_title"])}</title>', out, count=1, flags=re.S)
    out = re.sub(r'<meta name="description" content="[^"]*">',
                 f'<meta name="description" content="{esc_attr(tr["page_desc"])}">', out, count=1)
    out = out.replace('<link rel="canonical" href="https://openelpis.com/">',
                      f'<link rel="canonical" href="{BASE}{URL[lang]}">', 1)
    out = out.replace('<meta property="og:url" content="https://openelpis.com/">',
                      f'<meta property="og:url" content="{BASE}{URL[lang]}">', 1)
    out = out.replace('<meta property="og:locale" content="en_US">',
                      f'<meta property="og:locale" content="{LOCALE[lang]}">', 1)
    for prop in ("og:title", "twitter:title"):
        out = re.sub(rf'(property="{prop}"|name="{prop}") content="[^"]*"',
                     lambda mm: f'{mm.group(1)} content="{esc_attr(tr["page_title"])}"', out, count=1)
    for prop in ("og:description", "twitter:description"):
        out = re.sub(rf'(property="{prop}"|name="{prop}") content="[^"]*"',
                     lambda mm: f'{mm.group(1)} content="{esc_attr(tr["page_desc"])}"', out, count=1)
    out = out.replace('href="/sponsor/"', f'href="/{lang}/sponsor/"')   # localized sponsor page
    return out

for lang in LANGS:
    os.makedirs(lang, exist_ok=True)
    open(f"{lang}/index.html", "w", encoding="utf-8").write(localize(src, lang))
    print(f"wrote {lang}/index.html")

# ───────────────────────── 2. founder name-profile pages ─────────────────────
NAME2SLUG = {"Ali Tajbakhsh": "ali-tajbakhsh",
             "Mohammadali Tajbakhsh": "mohammadali-tajbakhsh",
             "Alan Tajbakhsh": "alan-tajbakhsh"}
PROFILES = [
    {"slug": "ali-tajbakhsh", "primary": "Ali Tajbakhsh",
     "others": ["Mohammadali Tajbakhsh", "Alan Tajbakhsh"]},
    {"slug": "mohammadali-tajbakhsh", "primary": "Mohammadali Tajbakhsh",
     "others": ["Ali Tajbakhsh", "Alan Tajbakhsh"]},
    {"slug": "alan-tajbakhsh", "primary": "Alan Tajbakhsh",
     "others": ["Ali Tajbakhsh", "Mohammadali Tajbakhsh"]},
]
PROF_LANGS = ["en", "tr"]                      # profiles only in EN + TR
ALTNAMES_ALL = ["Ali Tajbakhsh", "Mohammadali Tajbakhsh", "Mohammad Ali Tajbakhsh", "Alan Tajbakhsh"]

COPY = {
 "en": {
  "title": "{primary} — Founder of OpenElpis",
  "desc": "{primary} (also known as {o1} and {o2}) is the founder of OpenElpis, a non-profit, open-source AI research copilot for breast cancer. Technical Business Analyst & Project Manager based in Türkiye.",
  "eyebrow": "Founder of OpenElpis",
  "aka": "Also known as <b>{o1}</b> &middot; <b>{o2}</b>",
  "roles": ["Technical Business Analyst", "Project Manager", "Türkiye"],
  "p1": "<strong>{primary}</strong> is the founder of <a href=\"/\">OpenElpis</a> — an independent, non-profit, open-source initiative building a trustworthy, citation-grounded AI research copilot for breast cancer. {primary} is a Technical Business Analyst and Project Manager based in Türkiye.",
  "p2": "Online, {primary} also goes by <a href=\"{o1href}\">{o1}</a> and <a href=\"{o2href}\">{o2}</a> — all three names refer to the same person.",
  "quote": "I founded OpenElpis to try my best to help the people facing breast cancer — to make trustworthy, evidence-grounded knowledge open and free for everyone fighting for them.",
  "p3": "Connect with {primary} on LinkedIn and GitHub, or read the full story on the OpenElpis founder page.",
  "li": "LinkedIn", "gh": "GitHub", "em": "Email",
  "more": "OpenElpis founder page →", "home": "← OpenElpis home",
  "foot": "OpenElpis is an independent, non-profit initiative led by an individual and is not yet a registered legal entity.",
  "crumb_home": "OpenElpis",
 },
 "tr": {
  "title": "{primary} — OpenElpis Kurucusu",
  "desc": "{primary} ({o1} ve {o2} olarak da bilinir), meme kanseri için kâr amacı gütmeyen, açık kaynaklı bir yapay zeka araştırma yardımcısı olan OpenElpis'in kurucusudur. Türkiye'de yaşayan bir Teknik İş Analisti ve Proje Yöneticisidir.",
  "eyebrow": "OpenElpis Kurucusu",
  "aka": "Şu adlarla da bilinir: <b>{o1}</b> &middot; <b>{o2}</b>",
  "roles": ["Teknik İş Analisti", "Proje Yöneticisi", "Türkiye"],
  "p1": "<strong>{primary}</strong>, meme kanseri için güvenilir, kaynağa dayalı bir yapay zeka araştırma yardımcısı geliştiren bağımsız, kâr amacı gütmeyen ve açık kaynaklı bir girişim olan <a href=\"/\">OpenElpis</a>'in kurucusudur. {primary}, Türkiye'de yaşayan bir Teknik İş Analisti ve Proje Yöneticisidir.",
  "p2": "İnternette {primary} aynı zamanda <a href=\"{o1href}\">{o1}</a> ve <a href=\"{o2href}\">{o2}</a> adlarıyla da bilinir — üç ad da aynı kişiye aittir.",
  "quote": "OpenElpis'i, meme kanseriyle yüzleşen insanlara elimden gelenin en iyisini yaparak yardım etmek için kurdum — güvenilir, kanıta dayalı bilgiyi onlar için savaşan herkese açık ve ücretsiz kılmak için.",
  "p3": "{primary} ile LinkedIn ve GitHub üzerinden bağlantı kurun ya da OpenElpis kurucu sayfasında tüm hikâyeyi okuyun.",
  "li": "LinkedIn", "gh": "GitHub", "em": "E-posta",
  "more": "OpenElpis kurucu sayfası →", "home": "← OpenElpis ana sayfa",
  "foot": tr_all["tr"]["footer_entity"],
  "crumb_home": "OpenElpis",
 },
}

STYLE = """<style>
  :root{--ink:#13211e;--ink-soft:#42524d;--muted:#6c7d78;--teal:#0e7c7b;--teal-deep:#0a5b5a;--teal-dark:#073f3e;
    --mint:#e7f3f1;--mint-2:#f4faf8;--rose:#e35d8a;--rose-deep:#c43a6e;--rose-dark:#a82f5f;--rose-soft:#fdeef3;
    --line:#dde7e4;--bg:#fbfdfc;--shadow:0 1px 2px rgba(7,63,62,.04),0 12px 32px -12px rgba(7,63,62,.16)}
  *{box-sizing:border-box;margin:0;padding:0}html{scroll-behavior:smooth}
  body{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--ink);background:var(--bg);line-height:1.6;font-size:17px;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1000px;margin:0 auto;padding:0 24px}
  h1,h2,h3{font-family:'Fraunces',Georgia,serif;font-weight:500;line-height:1.12;letter-spacing:-.01em;color:var(--ink)}
  a{color:var(--teal-deep);text-decoration:none}
  .eyebrow{font-family:'Inter';font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--rose-deep)}
  header.nav{position:sticky;top:0;z-index:50;backdrop-filter:blur(12px);background:rgba(251,253,252,.85);border-bottom:1px solid var(--line)}
  .nav .wrap{display:flex;align-items:center;justify-content:space-between;height:64px}
  .brand{display:flex;align-items:center;gap:11px;font-family:'Fraunces';font-size:20px;font-weight:600;color:var(--ink)}
  .brand .mark{width:36px;height:36px;flex:none}
  .nav .home{font-size:14px;font-weight:500;color:var(--ink-soft)}.nav .home:hover{color:var(--teal-deep)}
  .hero{background:radial-gradient(900px 480px at 84% -10%,rgba(227,93,138,.16),transparent 60%),radial-gradient(820px 520px at 2% 8%,rgba(14,124,123,.14),transparent 55%),linear-gradient(180deg,#fff,var(--mint-2))}
  .hero .wrap{padding:54px 24px 48px}
  .crumb{font-size:13px;color:var(--muted);margin-bottom:22px}.crumb a{color:var(--muted)}
  .grid{display:grid;grid-template-columns:auto 1fr;gap:40px;align-items:center}
  .photo{width:190px;height:190px;border-radius:26px;object-fit:cover;display:block;border:4px solid #fff;box-shadow:0 26px 52px -22px rgba(7,63,62,.4),0 0 0 1px var(--line)}
  .badge{display:inline-flex;gap:8px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:999px;padding:6px 14px;font-size:13px;font-weight:600;color:var(--rose-deep);box-shadow:var(--shadow)}
  .badge .dot{width:7px;height:7px;border-radius:50%;background:var(--rose)}
  h1.name{font-family:'Sora',-apple-system,sans-serif;font-weight:800;letter-spacing:-.03em;line-height:1.03;font-size:clamp(32px,5vw,50px);margin:16px 0 0}
  .aka{margin-top:10px;font-size:15px;color:var(--muted)}.aka b{color:var(--ink-soft);font-weight:600}
  .roles{margin-top:16px;display:flex;flex-wrap:wrap;gap:8px 10px}
  .roles span{background:var(--mint);border:1px solid #cfe6e2;border-radius:999px;padding:5px 13px;font-size:14px;font-weight:600;color:var(--teal-deep)}
  .links{display:flex;flex-wrap:wrap;gap:11px;margin-top:24px}
  .slink{display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:600;padding:9px 16px;border-radius:999px;border:1px solid var(--line);background:#fff;color:var(--ink);transition:.18s}
  .slink:hover{transform:translateY(-1px);border-color:var(--teal);color:var(--teal-deep);box-shadow:var(--shadow)}
  .slink svg{width:17px;height:17px;flex:none}
  section.body{padding:48px 0}.body .wrap{max-width:760px}
  .body p{color:var(--ink-soft);font-size:18px;margin-bottom:18px}.body p strong{color:var(--ink)}
  .pull{margin:30px 0;padding:20px 26px;border-left:4px solid var(--rose-deep);background:var(--rose-soft);border-radius:0 14px 14px 0;font-family:'Fraunces',Georgia,serif;font-size:20px;font-style:italic;color:var(--ink);line-height:1.4}
  footer{background:#07302f;color:#cfe1de;padding:42px 0 36px;margin-top:10px}
  footer .wrap{display:flex;flex-wrap:wrap;gap:16px;justify-content:space-between;align-items:center}
  footer a{color:#9fc7c2}footer a:hover{color:#fff}
  .foot-note{font-size:13px;color:#7fa9a4;max-width:62ch;margin-top:14px}
  @media(max-width:680px){.grid{grid-template-columns:1fr;gap:26px;text-align:center}.photo{justify-self:center}.roles,.links{justify-content:center}}
</style>"""

LI_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M4.98 3.5C4.98 4.88 3.87 6 2.5 6S0 4.88 0 3.5 1.12 1 2.5 1 4.98 2.12 4.98 3.5zM.24 8h4.52v14H.24V8zm7.2 0h4.33v1.92h.06c.6-1.14 2.07-2.34 4.27-2.34 4.57 0 5.41 3 5.41 6.9V22h-4.52v-6.62c0-1.58-.03-3.6-2.2-3.6-2.2 0-2.54 1.72-2.54 3.49V22H7.44V8z"/></svg>'
GH_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 .5C5.37.5 0 5.87 0 12.5c0 5.3 3.44 9.8 8.21 11.39.6.11.82-.26.82-.58 0-.29-.01-1.04-.02-2.05-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.09 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49.99.11-.78.42-1.3.76-1.6-2.67-.3-5.47-1.34-5.47-5.95 0-1.32.47-2.39 1.24-3.23-.13-.3-.54-1.52.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 016 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.66.25 2.88.12 3.18.77.84 1.24 1.91 1.24 3.23 0 4.62-2.81 5.65-5.49 5.95.43.37.82 1.1.82 2.22 0 1.61-.02 2.9-.02 3.29 0 .32.22.7.83.58C20.57 22.29 24 17.79 24 12.5 24 5.87 18.63.5 12 .5z"/></svg>'
EM_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m2 7 10 6 10-6"/></svg>'

def prof_url(slug, lang):
    return f"/{slug}/" if lang == "en" else f"/{lang}/{slug}/"

def profile_html(p, lang):
    c = COPY[lang]
    primary, (o1, o2) = p["primary"], p["others"]
    o1href = prof_url(NAME2SLUG[o1], lang)
    o2href = prof_url(NAME2SLUG[o2], lang)
    fmt = dict(primary=primary, o1=o1, o2=o2, o1href=o1href, o2href=o2href)
    page_url = BASE + prof_url(p["slug"], lang)
    title = c["title"].format(**fmt)
    desc = c["desc"].format(**fmt)

    ld = {
      "@context": "https://schema.org", "@type": "ProfilePage", "dateModified": LASTMOD,
      "inLanguage": lang,
      "mainEntity": {
        "@type": "Person", "@id": "https://openelpis.com/#ali-tajbakhsh",
        "name": primary,
        "alternateName": [n for n in ALTNAMES_ALL if n != primary],
        "givenName": primary.split()[0], "familyName": "Tajbakhsh",
        "jobTitle": "Founder of OpenElpis · Technical Business Analyst & Project Manager",
        "description": desc, "image": f"{BASE}/ali-tajbakhsh.jpg", "url": page_url,
        "sameAs": ["https://www.linkedin.com/in/alan-tajbakhsh/",
                   "https://github.com/alitajbakhsh", f"{BASE}/founder/"],
        "worksFor": {"@type": "Organization", "name": "OpenElpis", "url": f"{BASE}/"},
        "homeLocation": {"@type": "Place", "name": "Türkiye"},
      }}
    crumb = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "OpenElpis", "item": f"{BASE}/"},
        {"@type": "ListItem", "position": 2, "name": primary, "item": page_url}]}
    ld_s = json.dumps(ld, ensure_ascii=False, indent=2)
    crumb_s = json.dumps(crumb, ensure_ascii=False, indent=2)

    # hreflang: en + tr + x-default(en)
    hl = "\n".join(
        [f'<link rel="alternate" hreflang="{l}" href="{BASE}{prof_url(p["slug"], l)}">' for l in PROF_LANGS]
        + [f'<link rel="alternate" hreflang="x-default" href="{BASE}{prof_url(p["slug"], "en")}">'])
    roles = "".join(f"<span>{esc_text(r)}</span>" for r in c["roles"])
    alt_img = f"{primary} — founder of OpenElpis (also known as {o1} and {o2})"

    head = (
      f'<!DOCTYPE html>\n<html lang="{lang}">\n<head>\n'
      f'<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
      f'<title>{esc_text(title)}</title>\n'
      f'<meta name="description" content="{esc_attr(desc)}">\n'
      f'<meta name="author" content="{esc_attr(primary)}">\n'
      f'<link rel="canonical" href="{page_url}">\n'
      f'<meta name="robots" content="index, follow, max-image-preview:large">\n{hl}\n'
      f'<meta property="og:type" content="profile">\n'
      f'<meta property="og:title" content="{esc_attr(title)}">\n'
      f'<meta property="og:description" content="{esc_attr(desc)}">\n'
      f'<meta property="og:image" content="{BASE}/ali-tajbakhsh.jpg">\n'
      f'<meta property="og:url" content="{page_url}">\n'
      f'<meta property="og:site_name" content="OpenElpis">\n'
      f'<meta property="og:locale" content="{LOCALE[lang]}">\n'
      f'<meta name="twitter:card" content="summary_large_image">\n'
      f'<meta name="twitter:title" content="{esc_attr(title)}">\n'
      f'<meta name="twitter:description" content="{esc_attr(desc)}">\n'
      f'<meta name="twitter:image" content="{BASE}/ali-tajbakhsh.jpg">\n'
      f'<meta name="theme-color" content="#0a5b5a">\n'
      f'<link rel="preconnect" href="https://fonts.googleapis.com">\n'
      f'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
      f'<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,ital,wght@9..144,1,400;9..144,0,400;9..144,0,500;9..144,0,600&family=Inter:wght@400;500;600;700&family=Sora:wght@600;700;800&display=swap" rel="stylesheet">\n'
      f'<link rel="icon" type="image/svg+xml" href="/logo.svg">\n'
      f'<script type="application/ld+json">\n{ld_s}\n</script>\n'
      f'<script type="application/ld+json">\n{crumb_s}\n</script>\n'
      f'{STYLE}\n</head>\n')

    body = (
      f'<body>\n'
      f'<header class="nav"><div class="wrap">'
      f'<a class="brand" href="/"><img class="mark" src="/logo.svg" alt="OpenElpis" width="36" height="36"> OpenElpis</a>'
      f'<a class="home" href="/founder/">{esc_text(c["more"])}</a></div></header>\n'
      f'<section class="hero"><div class="wrap">\n'
      f'<nav class="crumb" aria-label="Breadcrumb"><a href="/">{esc_text(c["crumb_home"])}</a> &nbsp;›&nbsp; {esc_text(primary)}</nav>\n'
      f'<div class="grid">\n'
      f'<img class="photo" src="/ali-tajbakhsh.jpg" width="190" height="190" alt="{esc_attr(alt_img)}">\n'
      f'<div>\n<span class="badge"><span class="dot"></span> {esc_text(c["eyebrow"])}</span>\n'
      f'<h1 class="name">{esc_text(primary)}</h1>\n'
      f'<p class="aka">{c["aka"].format(**fmt)}</p>\n'
      f'<div class="roles">{roles}</div>\n'
      f'<div class="links">\n'
      f'<a class="slink" href="https://www.linkedin.com/in/alan-tajbakhsh/" target="_blank" rel="me noopener">{LI_SVG} {esc_text(c["li"])}</a>\n'
      f'<a class="slink" href="https://github.com/alitajbakhsh" target="_blank" rel="me noopener">{GH_SVG} {esc_text(c["gh"])}</a>\n'
      f'<a class="slink" href="mailto:hello@openelpis.com">{EM_SVG} {esc_text(c["em"])}</a>\n'
      f'</div>\n</div>\n</div>\n</div></section>\n'
      f'<section class="body"><div class="wrap">\n'
      f'<p>{c["p1"].format(**fmt)}</p>\n'
      f'<p>{c["p2"].format(**fmt)}</p>\n'
      f'<div class="pull">“{esc_text(c["quote"])}”</div>\n'
      f'<p>{esc_text(c["p3"].format(**fmt))}</p>\n'
      f'<p><a href="/founder/">{esc_text(c["more"])}</a> &nbsp;·&nbsp; <a href="/">{esc_text(c["home"])}</a></p>\n'
      f'</div></section>\n'
      f'<footer><div class="wrap">'
      f'<a class="brand" href="/" style="color:#fff"><img class="mark" src="/logo.svg" alt="OpenElpis" width="36" height="36"> OpenElpis</a>'
      f'<a href="/founder/">{esc_text(c["more"])}</a>'
      f'</div><div class="wrap"><p class="foot-note">{esc_text(c["foot"])}</p></div></footer>\n'
      f'</body>\n</html>\n')
    return head + body

prof_paths = []
for p in PROFILES:
    for lang in PROF_LANGS:
        d = p["slug"] if lang == "en" else f"{lang}/{p['slug']}"
        os.makedirs(d, exist_ok=True)
        open(f"{d}/index.html", "w", encoding="utf-8").write(profile_html(p, lang))
        prof_paths.append(f"{d}/index.html")
        print(f"wrote {d}/index.html")

# ───────────────────────── 2b. sponsor / "how you can help" page (EN + TR) ────
import urllib.parse
SPONSOR_LANGS = ["en", "tr", "es", "de", "fr", "it", "ru", "nl"]
def sponsor_url(lang): return "/sponsor/" if lang == "en" else f"/{lang}/sponsor/"
def mailto(subject): return "mailto:hello@openelpis.com?subject=" + urllib.parse.quote(subject)

IC = {
 "server": '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><path d="M6 6h.01M6 18h.01"/></svg>',
 "clinic": '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 21h18M5 21V7l7-4 7 4v14"/><path d="M12 9v6M9 12h6"/></svg>',
 "flask": '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3v6L4 17a2 2 0 0 0 2 3h12a2 2 0 0 0 2-3l-5-8V3"/><path d="M8 3h8M7 16h10"/></svg>',
 "heart": '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l8.8 8.6 8.8-8.6a5.5 5.5 0 0 0 0-7.8z"/></svg>',
 "plus": '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/></svg>',
}

SPONSOR = {
 "en": {
  "title": "How you can help — Partner with OpenElpis",
  "desc": "Concrete ways to support OpenElpis, a non-profit, open-source AI research copilot for breast cancer: donate cloud, compute or servers; cooperate as a clinic or hospital; collaborate as a researcher; or help financially.",
  "badge": "Partner with OpenElpis", "h1": "How you can help",
  "lead": "OpenElpis runs on donated infrastructure and goodwill. It's a non-profit, open-source project — not yet a registered legal entity — building a trustworthy, citation-grounded AI research copilot for breast cancer, free for everyone. Here are the concrete ways you can help make it real.",
  "pdf": "Read the full proposal (PDF)", "email": "Email us",
  "ways_head": "Ways to help",
  "ways": [
    {"ic": "server", "h": "Cloud, compute & infrastructure providers",
     "p": "If your company provides cloud, hosting, storage, CDN or GPU compute, donated credits or free-tier capacity directly power the platform. Our Phase-1 footprint is small and portable (~$3,000–$5,000/yr retail value) — covering even one line item materially de-risks the project.",
     "ul": ["A 24/7 compute instance (~8 vCPU / 16–32 GB RAM / 200 GB NVMe)",
            "Object storage (250 GB – 2 TB, S3-compatible)",
            "CDN + WAF + DDoS protection in front of the public site",
            "Managed PostgreSQL (~2 vCPU / 8 GB / 100 GB)",
            "Bursty GPU compute (a few hundred A100/H100-class GPU-hours per year)"],
     "cta": "Offer infrastructure →", "subject": "Infrastructure support for OpenElpis"},
    {"ic": "clinic", "h": "Doctors, clinics & hospitals",
     "p": "Clinical partners are the heart of the trust pipeline. You can help validate what goes in and guide what we build — with no patient-identifiable data involved in the early phases.",
     "ul": ["Become a verified contributor or expert reviewer of breast-cancer literature and findings",
            "Share de-identified, aggregate findings through a governed, ethics-first pipeline (deferred until data-use agreements and IRB oversight are in place)",
            "Advise on clinical relevance, safety, and the real research questions worth answering",
            "Help shape data-governance and review standards"],
     "cta": "Cooperate clinically →", "subject": "Clinical cooperation with OpenElpis"},
    {"ic": "flask", "h": "Researchers & academic institutions",
     "p": "Researchers and labs make the corpus worth trusting.",
     "ul": ["Contribute validated literature, de-identified datasets, or structured findings",
            "Peer-review submissions and advise on the curated corpus",
            "Collaborate on methods, evaluation, and open publications",
            "Connect us with relevant programs, grants, or communities"],
     "cta": "Collaborate →", "subject": "Research collaboration with OpenElpis"},
    {"ic": "heart", "h": "Individuals who want to help financially",
     "p": "OpenElpis is an independent, non-profit project and is not yet a registered legal entity, so there's no public donation button yet. If you'd like to support it financially, get in touch — every offer is handled openly and transparently.",
     "ul": ["Pledge to cover a specific running cost (e.g. a month of server or storage)",
            "Register your interest to donate once a registered non-profit / fiscal sponsor is in place",
            "Sponsor a milestone or a specific feature"],
     "cta": "Help financially →", "subject": "Supporting OpenElpis financially"},
    {"ic": "plus", "h": "Other ways to help",
     "p": "Not in one of the boxes above? There's still plenty you can do.",
     "ul": ["Contribute to the open-source code on GitHub",
            "Introduce us to a cloud / nonprofit-credits program or a potential funder",
            "Offer pro-bono help (legal, design, nonprofit formation, translation)",
            "Simply spread the word"],
     "cta": "Get in touch →", "subject": "Helping OpenElpis"},
  ],
  "returns_head": "What partners receive in return",
  "returns": ["Founding-partner recognition on openelpis.com, our GitHub, and release notes",
              "A real impact case study — a co-authored blog post on powering open breast-cancer research",
              "Periodic impact reports: contributors onboarded, corpus size, researcher queries served, datasets published",
              "Optional co-marketing around milestones, fully at your discretion"],
  "risk_head": "Why supporting OpenElpis is low-risk",
  "risk": ["No raw patient data by design in the early phases — published literature and de-identified, aggregate material only",
           "Human-validated, auditable, and retractable — not live-scraped, and never trained directly on uploads",
           "Open-source and portable across providers — no proprietary lock-in",
           "Clear liability framing: a research-support tool for professionals, never diagnosis or treatment advice"],
  "final_h": "Ready to help? Let's talk.",
  "final_p": "Tell us how you'd like to support OpenElpis — infrastructure, clinical cooperation, research, or funding — and we'll take it from there.",
  "final_email": "Email hello@openelpis.com", "final_pdf": "Download the proposal (PDF)",
  "home": "← Back to OpenElpis", "crumb": "OpenElpis",
 },
 "tr": {
  "title": "Nasıl yardım edebilirsiniz — OpenElpis'e ortak olun",
  "desc": "OpenElpis'i desteklemenin somut yolları: bulut/işlem gücü/sunucu bağışlayın, klinik veya hastane olarak iş birliği yapın, araştırmacı olarak katkı verin ya da maddi olarak yardım edin. Meme kanseri için kâr amacı gütmeyen, açık kaynaklı bir yapay zeka araştırma yardımcısı.",
  "badge": "OpenElpis ortağı olun", "h1": "Nasıl yardım edebilirsiniz",
  "lead": "OpenElpis, bağışlanan altyapı ve iyi niyetle çalışır. Kâr amacı gütmeyen, açık kaynaklı bir projedir — henüz kayıtlı bir tüzel kişilik değildir — ve meme kanseri için güvenilir, kaynağa dayalı, herkese açık ve ücretsiz bir yapay zeka araştırma yardımcısı geliştiriyor. İşte bunu gerçeğe dönüştürmenize yardımcı olabilecek somut yollar.",
  "pdf": "Tam teklifi okuyun (PDF)", "email": "Bize e-posta gönderin",
  "ways_head": "Yardım yolları",
  "ways": [
    {"ic": "server", "h": "Bulut, işlem gücü ve altyapı sağlayıcıları",
     "p": "Şirketiniz bulut, sunucu, depolama, CDN veya GPU işlem gücü sağlıyorsa, bağışlanan krediler veya ücretsiz kapsam doğrudan platforma güç verir. Faz 1 ihtiyacımız küçük ve taşınabilirdir (yıllık ~3.000–5.000 ABD doları perakende değer) — tek bir kalemi karşılamak bile projeyi önemli ölçüde güvenceye alır.",
     "ul": ["7/24 çalışan bir sunucu (~8 vCPU / 16–32 GB RAM / 200 GB NVMe)",
            "Nesne depolama (250 GB – 2 TB, S3 uyumlu)",
            "Genel sitenin önünde CDN + WAF + DDoS koruması",
            "Yönetilen PostgreSQL (~2 vCPU / 8 GB / 100 GB)",
            "Aralıklı GPU işlem gücü (yılda birkaç yüz A100/H100 sınıfı GPU saati)"],
     "cta": "Altyapı sağlayın →", "subject": "OpenElpis için altyapı desteği"},
    {"ic": "clinic", "h": "Doktorlar, klinikler ve hastaneler",
     "p": "Klinik ortaklar, güven hattının kalbidir. Sisteme neyin girdiğini doğrulamaya ve ne geliştirdiğimize yön vermeye yardımcı olabilirsiniz — erken aşamalarda hasta kimliği belirlenebilir hiçbir veri kullanılmadan.",
     "ul": ["Meme kanseri literatürünün ve bulgularının doğrulanmış katkıcısı veya uzman değerlendiricisi olun",
            "Etik öncelikli, yönetişimli bir hat üzerinden kimliksizleştirilmiş, toplu bulgular paylaşın (veri kullanım anlaşmaları ve IRB gözetimi sağlanana kadar ertelenir)",
            "Klinik uygunluk, güvenlik ve yanıtlanmaya değer gerçek araştırma soruları konusunda yol gösterin",
            "Veri yönetişimi ve değerlendirme standartlarının şekillenmesine yardım edin"],
     "cta": "Klinik olarak iş birliği yapın →", "subject": "OpenElpis ile klinik iş birliği"},
    {"ic": "flask", "h": "Araştırmacılar ve akademik kurumlar",
     "p": "Araştırmacılar ve laboratuvarlar, külliyatı güvenilir kılan taraftır.",
     "ul": ["Doğrulanmış literatür, kimliksizleştirilmiş veri kümeleri veya yapılandırılmış bulgular sağlayın",
            "Gönderimleri akran değerlendirmesinden geçirin ve derlenen külliyat hakkında görüş bildirin",
            "Yöntemler, değerlendirme ve açık yayınlar üzerinde iş birliği yapın",
            "Bizi ilgili programlar, hibeler veya topluluklarla buluşturun"],
     "cta": "İş birliği yapın →", "subject": "OpenElpis ile araştırma iş birliği"},
    {"ic": "heart", "h": "Maddi olarak yardım etmek isteyen bireyler",
     "p": "OpenElpis bağımsız, kâr amacı gütmeyen bir projedir ve henüz kayıtlı bir tüzel kişilik değildir; bu nedenle henüz herkese açık bir bağış düğmesi yoktur. Maddi destek vermek isterseniz bizimle iletişime geçin — her teklif açık ve şeffaf bir şekilde ele alınır.",
     "ul": ["Belirli bir işletme masrafını karşılamayı taahhüt edin (örn. bir aylık sunucu veya depolama)",
            "Kayıtlı bir kâr amacı gütmeyen kuruluş / mali sponsor kurulduğunda bağış yapmak için ilginizi bildirin",
            "Bir kilometre taşına veya belirli bir özelliğe sponsor olun"],
     "cta": "Maddi olarak yardım edin →", "subject": "OpenElpis'e maddi destek"},
    {"ic": "plus", "h": "Diğer yardım yolları",
     "p": "Yukarıdaki kutulardan birine girmiyor musunuz? Yine de yapabileceğiniz çok şey var.",
     "ul": ["GitHub'daki açık kaynak koda katkıda bulunun",
            "Bizi bir bulut / kâr amacı gütmeyen kredi programıyla veya olası bir fon sağlayıcıyla tanıştırın",
            "Gönüllü (pro-bono) destek sunun (hukuk, tasarım, dernek kuruluşu, çeviri)",
            "Sadece haberi yayın"],
     "cta": "İletişime geçin →", "subject": "OpenElpis'e yardım"},
  ],
  "returns_head": "Ortakların karşılığında elde ettikleri",
  "returns": ["openelpis.com'da, GitHub'da ve sürüm notlarında kurucu ortak olarak tanınma",
              "Gerçek bir etki vaka çalışması — açık meme kanseri araştırmalarına güç vermeye dair ortak yazılmış bir blog yazısı",
              "Düzenli etki raporları: katılan katkıcılar, külliyat boyutu, yanıtlanan araştırmacı sorguları, açık yayımlanan veri kümeleri",
              "Kilometre taşları etrafında, tamamen sizin takdirinize bağlı isteğe bağlı ortak pazarlama"],
  "risk_head": "OpenElpis'i desteklemek neden düşük risklidir",
  "risk": ["Erken aşamalarda tasarım gereği ham hasta verisi yok — yalnızca yayımlanmış literatür ve kimliksizleştirilmiş, toplu materyal",
           "İnsan tarafından doğrulanmış, denetlenebilir ve geri çekilebilir — canlı taranmaz ve asla doğrudan yüklemelerle eğitilmez",
           "Açık kaynaklı ve sağlayıcılar arasında taşınabilir — tescilli bağımlılık yok",
           "Net sorumluluk çerçevesi: profesyoneller için bir araştırma destek aracı; asla teşhis veya tedavi önerisi değil"],
  "final_h": "Yardım etmeye hazır mısınız? Konuşalım.",
  "final_p": "OpenElpis'i nasıl desteklemek istediğinizi bize bildirin — altyapı, klinik iş birliği, araştırma veya finansman — gerisini birlikte hallederiz.",
  "final_email": "hello@openelpis.com'a e-posta gönderin", "final_pdf": "Teklifi indirin (PDF)",
  "home": "← OpenElpis'e dön", "crumb": "OpenElpis",
 },
 "es": {
  "title": "Cómo puedes ayudar — Colabora con OpenElpis",
  "desc": "Formas concretas de apoyar a OpenElpis, un copiloto de investigación con IA, sin ánimo de lucro y de código abierto, para el cáncer de mama: dona nube, cómputo o servidores; coopera como clínica u hospital; colabora como investigador; o ayuda económicamente.",
  "badge": "Colabora con OpenElpis", "h1": "Cómo puedes ayudar",
  "lead": "OpenElpis funciona con infraestructura donada y buena voluntad. Es un proyecto sin ánimo de lucro y de código abierto —aún no es una entidad jurídica registrada— que construye un copiloto de investigación con IA confiable y fundamentado en citas para el cáncer de mama, gratuito para todos. Estas son las formas concretas en que puedes ayudar a hacerlo realidad.",
  "pdf": "Lee la propuesta completa (PDF)", "email": "Escríbenos",
  "ways_head": "Formas de ayudar",
  "ways": [
    {"ic": "server", "h": "Proveedores de nube, cómputo e infraestructura",
     "p": "Si tu empresa ofrece nube, hosting, almacenamiento, CDN o cómputo GPU, los créditos donados o la capacidad gratuita impulsan directamente la plataforma. Nuestra huella de la Fase 1 es pequeña y portátil (~3.000–5.000 USD/año de valor de mercado); cubrir incluso una sola línea reduce considerablemente el riesgo del proyecto.",
     "ul": ["Una instancia de cómputo 24/7 (~8 vCPU / 16–32 GB de RAM / 200 GB NVMe)",
            "Almacenamiento de objetos (250 GB – 2 TB, compatible con S3)",
            "CDN + WAF + protección DDoS delante del sitio público",
            "PostgreSQL gestionado (~2 vCPU / 8 GB / 100 GB)",
            "Cómputo GPU puntual (unos cientos de horas-GPU de clase A100/H100 al año)"],
     "cta": "Ofrecer infraestructura →", "subject": "Apoyo de infraestructura para OpenElpis"},
    {"ic": "clinic", "h": "Médicos, clínicas y hospitales",
     "p": "Los socios clínicos son el corazón del proceso de confianza. Puedes ayudar a validar lo que entra y a orientar lo que construimos, sin que se utilice ningún dato identificable de pacientes en las primeras fases.",
     "ul": ["Conviértete en colaborador verificado o revisor experto de literatura y hallazgos sobre cáncer de mama",
            "Comparte hallazgos anonimizados y agregados a través de un proceso gobernado y con la ética por delante (aplazado hasta contar con acuerdos de uso de datos y supervisión de un comité de ética/IRB)",
            "Asesora sobre relevancia clínica, seguridad y las preguntas de investigación que realmente vale la pena responder",
            "Ayuda a definir los estándares de gobernanza de datos y de revisión"],
     "cta": "Cooperar clínicamente →", "subject": "Cooperación clínica con OpenElpis"},
    {"ic": "flask", "h": "Investigadores e instituciones académicas",
     "p": "Los investigadores y los laboratorios hacen que el corpus merezca confianza.",
     "ul": ["Aporta literatura validada, conjuntos de datos anonimizados o hallazgos estructurados",
            "Revisa por pares las contribuciones y asesora sobre el corpus curado",
            "Colabora en métodos, evaluación y publicaciones abiertas",
            "Conéctanos con programas, becas o comunidades relevantes"],
     "cta": "Colaborar →", "subject": "Colaboración de investigación con OpenElpis"},
    {"ic": "heart", "h": "Personas que quieren ayudar económicamente",
     "p": "OpenElpis es un proyecto independiente y sin ánimo de lucro y todavía no es una entidad jurídica registrada, así que aún no hay un botón público de donación. Si quieres apoyarlo económicamente, ponte en contacto: cada ofrecimiento se gestiona de forma abierta y transparente.",
     "ul": ["Comprométete a cubrir un coste concreto (p. ej., un mes de servidor o almacenamiento)",
            "Registra tu interés en donar cuando exista una entidad sin ánimo de lucro registrada / un patrocinador fiscal",
            "Patrocina un hito o una función específica"],
     "cta": "Ayudar económicamente →", "subject": "Apoyo económico a OpenElpis"},
    {"ic": "plus", "h": "Otras formas de ayudar",
     "p": "¿No encajas en ninguna de las casillas anteriores? Aun así puedes hacer mucho.",
     "ul": ["Contribuye al código de código abierto en GitHub",
            "Preséntanos a un programa de créditos de nube / para organizaciones sin ánimo de lucro o a un posible financiador",
            "Ofrece ayuda pro bono (legal, diseño, constitución de la entidad, traducción)",
            "Simplemente, corre la voz"],
     "cta": "Ponte en contacto →", "subject": "Ayudar a OpenElpis"},
  ],
  "returns_head": "Qué reciben los socios a cambio",
  "returns": ["Reconocimiento como socio fundador en openelpis.com, nuestro GitHub y las notas de versión",
              "Un caso de impacto real: una entrada de blog en coautoría sobre impulsar la investigación abierta del cáncer de mama",
              "Informes de impacto periódicos: colaboradores incorporados, tamaño del corpus, consultas de investigadores atendidas, conjuntos de datos publicados",
              "Co-marketing opcional en torno a los hitos, totalmente a tu discreción"],
  "risk_head": "Por qué apoyar OpenElpis es de bajo riesgo",
  "risk": ["Sin datos brutos de pacientes por diseño en las primeras fases: solo literatura publicada y material anonimizado y agregado",
           "Validado por humanos, auditable y retractable: no se extrae en tiempo real ni se entrena nunca directamente con las aportaciones",
           "De código abierto y portátil entre proveedores: sin dependencia de tecnología propietaria",
           "Marco de responsabilidad claro: una herramienta de apoyo a la investigación para profesionales, nunca diagnóstico ni recomendaciones de tratamiento"],
  "final_h": "¿Listo para ayudar? Hablemos.",
  "final_p": "Cuéntanos cómo te gustaría apoyar a OpenElpis —infraestructura, cooperación clínica, investigación o financiación— y nos encargamos del resto.",
  "final_email": "Escribe a hello@openelpis.com", "final_pdf": "Descarga la propuesta (PDF)",
  "home": "← Volver a OpenElpis", "crumb": "OpenElpis",
 },
 "de": {
  "title": "Wie Sie helfen können — Werden Sie Partner von OpenElpis",
  "desc": "Konkrete Möglichkeiten, OpenElpis zu unterstützen, einen gemeinnützigen, quelloffenen KI-Forschungs-Copiloten für Brustkrebs: spenden Sie Cloud, Rechenleistung oder Server; kooperieren Sie als Klinik oder Krankenhaus; arbeiten Sie als Forschende mit; oder helfen Sie finanziell.",
  "badge": "Werden Sie Partner von OpenElpis", "h1": "Wie Sie helfen können",
  "lead": "OpenElpis lebt von gespendeter Infrastruktur und gutem Willen. Es ist ein gemeinnütziges, quelloffenes Projekt — noch keine eingetragene juristische Person — und entwickelt einen vertrauenswürdigen, quellenbasierten KI-Forschungs-Copiloten für Brustkrebs, kostenlos für alle. Hier sind die konkreten Möglichkeiten, wie Sie helfen können, das Wirklichkeit werden zu lassen.",
  "pdf": "Vollständigen Vorschlag lesen (PDF)", "email": "Schreiben Sie uns",
  "ways_head": "Möglichkeiten zu helfen",
  "ways": [
    {"ic": "server", "h": "Cloud-, Rechen- und Infrastrukturanbieter",
     "p": "Wenn Ihr Unternehmen Cloud, Hosting, Speicher, CDN oder GPU-Rechenleistung anbietet, treiben gespendete Credits oder Freikontingente die Plattform direkt an. Unser Phase-1-Bedarf ist klein und portabel (~3.000–5.000 USD/Jahr Marktwert) — schon das Abdecken einer einzigen Position senkt das Projektrisiko erheblich.",
     "ul": ["Eine rund um die Uhr laufende Recheninstanz (~8 vCPU / 16–32 GB RAM / 200 GB NVMe)",
            "Objektspeicher (250 GB – 2 TB, S3-kompatibel)",
            "CDN + WAF + DDoS-Schutz vor der öffentlichen Website",
            "Verwaltetes PostgreSQL (~2 vCPU / 8 GB / 100 GB)",
            "Sporadische GPU-Rechenleistung (einige Hundert GPU-Stunden der A100/H100-Klasse pro Jahr)"],
     "cta": "Infrastruktur anbieten →", "subject": "Infrastruktur-Unterstützung für OpenElpis"},
    {"ic": "clinic", "h": "Ärztinnen und Ärzte, Kliniken und Krankenhäuser",
     "p": "Klinische Partner sind das Herz der Vertrauenspipeline. Sie können helfen zu validieren, was hineinkommt, und mitgestalten, was wir bauen — ohne dass in den frühen Phasen patientenidentifizierende Daten verwendet werden.",
     "ul": ["Werden Sie verifizierte Mitwirkende oder Fachgutachter für Brustkrebs-Literatur und -Befunde",
            "Teilen Sie anonymisierte, aggregierte Befunde über eine geregelte, ethikorientierte Pipeline (verschoben, bis Datennutzungsvereinbarungen und IRB-/Ethik-Aufsicht vorhanden sind)",
            "Beraten Sie zu klinischer Relevanz, Sicherheit und den wirklich lohnenden Forschungsfragen",
            "Helfen Sie, Standards für Daten-Governance und Begutachtung zu gestalten"],
     "cta": "Klinisch kooperieren →", "subject": "Klinische Kooperation mit OpenElpis"},
    {"ic": "flask", "h": "Forschende und akademische Einrichtungen",
     "p": "Forschende und Labore machen den Korpus vertrauenswürdig.",
     "ul": ["Tragen Sie validierte Literatur, anonymisierte Datensätze oder strukturierte Befunde bei",
            "Begutachten Sie Einreichungen und beraten Sie zum kuratierten Korpus",
            "Arbeiten Sie an Methoden, Evaluierung und offenen Publikationen mit",
            "Vernetzen Sie uns mit relevanten Programmen, Förderungen oder Communities"],
     "cta": "Mitarbeiten →", "subject": "Forschungskooperation mit OpenElpis"},
    {"ic": "heart", "h": "Privatpersonen, die finanziell helfen möchten",
     "p": "OpenElpis ist ein unabhängiges, gemeinnütziges Projekt und noch keine eingetragene juristische Person, daher gibt es noch keinen öffentlichen Spenden-Button. Wenn Sie es finanziell unterstützen möchten, melden Sie sich — jedes Angebot wird offen und transparent behandelt.",
     "ul": ["Sagen Sie zu, eine bestimmte laufende Kostenposition zu übernehmen (z. B. einen Monat Server oder Speicher)",
            "Bekunden Sie Ihr Interesse zu spenden, sobald eine eingetragene gemeinnützige Organisation / ein Fiscal Sponsor vorhanden ist",
            "Sponsern Sie einen Meilenstein oder eine bestimmte Funktion"],
     "cta": "Finanziell helfen →", "subject": "OpenElpis finanziell unterstützen"},
    {"ic": "plus", "h": "Weitere Möglichkeiten zu helfen",
     "p": "Sie passen in keine der obigen Kategorien? Es gibt trotzdem viel, was Sie tun können.",
     "ul": ["Tragen Sie zum Open-Source-Code auf GitHub bei",
            "Stellen Sie uns einem Cloud-/Gemeinnützigkeits-Credits-Programm oder einem möglichen Förderer vor",
            "Bieten Sie Pro-bono-Hilfe an (Recht, Design, Vereinsgründung, Übersetzung)",
            "Erzählen Sie einfach weiter davon"],
     "cta": "Kontakt aufnehmen →", "subject": "OpenElpis helfen"},
  ],
  "returns_head": "Was Partner im Gegenzug erhalten",
  "returns": ["Anerkennung als Gründungspartner auf openelpis.com, unserem GitHub und in den Release Notes",
              "Eine echte Wirkungsgeschichte — ein gemeinsam verfasster Blogbeitrag über die Förderung offener Brustkrebsforschung",
              "Regelmäßige Wirkungsberichte: aufgenommene Mitwirkende, Korpusgröße, beantwortete Forschungsanfragen, veröffentlichte Datensätze",
              "Optionales Co-Marketing rund um Meilensteine, ganz nach Ihrem Ermessen"],
  "risk_head": "Warum die Unterstützung von OpenElpis risikoarm ist",
  "risk": ["Keine Rohdaten von Patienten in den frühen Phasen — nur veröffentlichte Literatur und anonymisiertes, aggregiertes Material",
           "Von Menschen validiert, prüfbar und widerrufbar — nicht live gescrapt und nie direkt mit Uploads trainiert",
           "Quelloffen und über Anbieter hinweg portabel — kein proprietärer Lock-in",
           "Klarer Haftungsrahmen: ein Forschungsunterstützungs-Werkzeug für Fachleute, niemals Diagnose oder Behandlungsempfehlung"],
  "final_h": "Bereit zu helfen? Sprechen wir.",
  "final_p": "Sagen Sie uns, wie Sie OpenElpis unterstützen möchten — Infrastruktur, klinische Kooperation, Forschung oder Finanzierung — und wir kümmern uns um den Rest.",
  "final_email": "E-Mail an hello@openelpis.com", "final_pdf": "Vorschlag herunterladen (PDF)",
  "home": "← Zurück zu OpenElpis", "crumb": "OpenElpis",
 },
 "fr": {
  "title": "Comment vous pouvez aider — Devenez partenaire d'OpenElpis",
  "desc": "Des façons concrètes de soutenir OpenElpis, un copilote de recherche par IA à but non lucratif et open source pour le cancer du sein : donnez du cloud, du calcul ou des serveurs ; coopérez en tant que clinique ou hôpital ; collaborez en tant que chercheur ; ou aidez financièrement.",
  "badge": "Devenez partenaire d'OpenElpis", "h1": "Comment vous pouvez aider",
  "lead": "OpenElpis fonctionne grâce à une infrastructure offerte et à la bienveillance. C'est un projet à but non lucratif et open source — pas encore une entité juridique enregistrée — qui développe un copilote de recherche par IA digne de confiance et fondé sur des citations pour le cancer du sein, gratuit pour tous. Voici les façons concrètes dont vous pouvez aider à le concrétiser.",
  "pdf": "Lire la proposition complète (PDF)", "email": "Écrivez-nous",
  "ways_head": "Façons d'aider",
  "ways": [
    {"ic": "server", "h": "Fournisseurs de cloud, de calcul et d'infrastructure",
     "p": "Si votre entreprise fournit du cloud, de l'hébergement, du stockage, du CDN ou du calcul GPU, des crédits offerts ou une capacité gratuite alimentent directement la plateforme. Notre empreinte de Phase 1 est modeste et portable (~3 000–5 000 USD/an en valeur marchande) — couvrir ne serait-ce qu'une seule ligne réduit considérablement le risque du projet.",
     "ul": ["Une instance de calcul 24/7 (~8 vCPU / 16–32 Go de RAM / 200 Go NVMe)",
            "Stockage d'objets (250 Go – 2 To, compatible S3)",
            "CDN + WAF + protection DDoS devant le site public",
            "PostgreSQL géré (~2 vCPU / 8 Go / 100 Go)",
            "Calcul GPU ponctuel (quelques centaines d'heures-GPU de classe A100/H100 par an)"],
     "cta": "Offrir de l'infrastructure →", "subject": "Soutien en infrastructure pour OpenElpis"},
    {"ic": "clinic", "h": "Médecins, cliniques et hôpitaux",
     "p": "Les partenaires cliniques sont au cœur du pipeline de confiance. Vous pouvez aider à valider ce qui entre et à orienter ce que nous construisons — sans aucune donnée identifiant les patients dans les premières phases.",
     "ul": ["Devenez contributeur vérifié ou relecteur expert de la littérature et des résultats sur le cancer du sein",
            "Partagez des résultats anonymisés et agrégés via un pipeline encadré et axé sur l'éthique (différé jusqu'à la mise en place d'accords d'utilisation des données et d'une supervision IRB/éthique)",
            "Conseillez sur la pertinence clinique, la sécurité et les vraies questions de recherche qui méritent d'être traitées",
            "Aidez à façonner les normes de gouvernance des données et de relecture"],
     "cta": "Coopérer cliniquement →", "subject": "Coopération clinique avec OpenElpis"},
    {"ic": "flask", "h": "Chercheurs et institutions académiques",
     "p": "Les chercheurs et les laboratoires rendent le corpus digne de confiance.",
     "ul": ["Contribuez de la littérature validée, des jeux de données anonymisés ou des résultats structurés",
            "Évaluez les soumissions par les pairs et conseillez sur le corpus sélectionné",
            "Collaborez sur les méthodes, l'évaluation et les publications ouvertes",
            "Mettez-nous en relation avec des programmes, des financements ou des communautés pertinents"],
     "cta": "Collaborer →", "subject": "Collaboration de recherche avec OpenElpis"},
    {"ic": "heart", "h": "Particuliers souhaitant aider financièrement",
     "p": "OpenElpis est un projet indépendant et à but non lucratif qui n'est pas encore une entité juridique enregistrée ; il n'y a donc pas encore de bouton de don public. Si vous souhaitez le soutenir financièrement, contactez-nous — chaque proposition est traitée de façon ouverte et transparente.",
     "ul": ["Engagez-vous à couvrir un coût précis (p. ex. un mois de serveur ou de stockage)",
            "Faites part de votre intérêt à faire un don une fois qu'une association enregistrée / un parrain fiscal sera en place",
            "Parrainez un jalon ou une fonctionnalité précise"],
     "cta": "Aider financièrement →", "subject": "Soutenir financièrement OpenElpis"},
    {"ic": "plus", "h": "Autres façons d'aider",
     "p": "Vous ne rentrez dans aucune des cases ci-dessus ? Vous pouvez quand même faire beaucoup.",
     "ul": ["Contribuez au code open source sur GitHub",
            "Présentez-nous à un programme de crédits cloud / pour associations ou à un financeur potentiel",
            "Offrez une aide bénévole (juridique, design, création de l'association, traduction)",
            "Faites simplement passer le mot"],
     "cta": "Nous contacter →", "subject": "Aider OpenElpis"},
  ],
  "returns_head": "Ce que les partenaires reçoivent en retour",
  "returns": ["Reconnaissance en tant que partenaire fondateur sur openelpis.com, notre GitHub et les notes de version",
              "Une véritable étude de cas d'impact — un article de blog co-écrit sur le soutien à la recherche ouverte sur le cancer du sein",
              "Des rapports d'impact réguliers : contributeurs intégrés, taille du corpus, requêtes de chercheurs traitées, jeux de données publiés",
              "Co-marketing optionnel autour des jalons, entièrement à votre discrétion"],
  "risk_head": "Pourquoi soutenir OpenElpis présente peu de risques",
  "risk": ["Aucune donnée brute de patients par conception dans les premières phases — uniquement de la littérature publiée et du matériel anonymisé et agrégé",
           "Validé par des humains, auditable et rétractable — non extrait en direct et jamais entraîné directement sur les contributions",
           "Open source et portable entre fournisseurs — aucun verrouillage propriétaire",
           "Cadre de responsabilité clair : un outil d'aide à la recherche pour les professionnels, jamais de diagnostic ni de conseil thérapeutique"],
  "final_h": "Prêt à aider ? Discutons-en.",
  "final_p": "Dites-nous comment vous souhaitez soutenir OpenElpis — infrastructure, coopération clinique, recherche ou financement — et nous nous occupons du reste.",
  "final_email": "Écrire à hello@openelpis.com", "final_pdf": "Télécharger la proposition (PDF)",
  "home": "← Retour à OpenElpis", "crumb": "OpenElpis",
 },
 "it": {
  "title": "Come puoi aiutare — Diventa partner di OpenElpis",
  "desc": "Modi concreti per sostenere OpenElpis, un copilota di ricerca con IA non-profit e open-source per il cancro al seno: dona cloud, calcolo o server; coopera come clinica o ospedale; collabora come ricercatore; o aiuta economicamente.",
  "badge": "Diventa partner di OpenElpis", "h1": "Come puoi aiutare",
  "lead": "OpenElpis funziona grazie a infrastrutture donate e buona volontà. È un progetto non-profit e open-source — non ancora un'entità giuridica registrata — che costruisce un copilota di ricerca con IA affidabile e fondato su citazioni per il cancro al seno, gratuito per tutti. Ecco i modi concreti in cui puoi aiutare a renderlo realtà.",
  "pdf": "Leggi la proposta completa (PDF)", "email": "Scrivici",
  "ways_head": "Modi per aiutare",
  "ways": [
    {"ic": "server", "h": "Fornitori di cloud, calcolo e infrastruttura",
     "p": "Se la tua azienda fornisce cloud, hosting, archiviazione, CDN o calcolo GPU, i crediti donati o la capacità gratuita alimentano direttamente la piattaforma. La nostra impronta della Fase 1 è piccola e portabile (~3.000–5.000 USD/anno di valore di mercato): coprire anche una sola voce riduce sensibilmente il rischio del progetto.",
     "ul": ["Un'istanza di calcolo attiva 24/7 (~8 vCPU / 16–32 GB di RAM / 200 GB NVMe)",
            "Archiviazione a oggetti (250 GB – 2 TB, compatibile con S3)",
            "CDN + WAF + protezione DDoS davanti al sito pubblico",
            "PostgreSQL gestito (~2 vCPU / 8 GB / 100 GB)",
            "Calcolo GPU occasionale (qualche centinaio di ore-GPU di classe A100/H100 all'anno)"],
     "cta": "Offri infrastruttura →", "subject": "Supporto infrastrutturale per OpenElpis"},
    {"ic": "clinic", "h": "Medici, cliniche e ospedali",
     "p": "I partner clinici sono il cuore della catena di fiducia. Puoi aiutare a validare ciò che entra e a orientare ciò che costruiamo, senza che venga utilizzato alcun dato identificativo dei pazienti nelle prime fasi.",
     "ul": ["Diventa contributore verificato o revisore esperto della letteratura e dei risultati sul cancro al seno",
            "Condividi risultati anonimizzati e aggregati tramite una pipeline governata e orientata all'etica (rinviata fino alla presenza di accordi sull'uso dei dati e supervisione IRB/etica)",
            "Fornisci consulenza su rilevanza clinica, sicurezza e le vere domande di ricerca a cui vale la pena rispondere",
            "Aiuta a definire gli standard di governance dei dati e di revisione"],
     "cta": "Collabora clinicamente →", "subject": "Cooperazione clinica con OpenElpis"},
    {"ic": "flask", "h": "Ricercatori e istituzioni accademiche",
     "p": "I ricercatori e i laboratori rendono il corpus degno di fiducia.",
     "ul": ["Contribuisci con letteratura validata, set di dati anonimizzati o risultati strutturati",
            "Esamina le proposte tramite revisione paritaria e fornisci consulenza sul corpus curato",
            "Collabora su metodi, valutazione e pubblicazioni aperte",
            "Mettici in contatto con programmi, finanziamenti o comunità pertinenti"],
     "cta": "Collabora →", "subject": "Collaborazione di ricerca con OpenElpis"},
    {"ic": "heart", "h": "Privati che vogliono aiutare economicamente",
     "p": "OpenElpis è un progetto indipendente e non-profit e non è ancora un'entità giuridica registrata, quindi non c'è ancora un pulsante di donazione pubblico. Se desideri sostenerlo economicamente, contattaci: ogni offerta è gestita in modo aperto e trasparente.",
     "ul": ["Impegnati a coprire un costo specifico (es. un mese di server o archiviazione)",
            "Registra il tuo interesse a donare quando sarà attiva un'organizzazione non-profit registrata / uno sponsor fiscale",
            "Sponsorizza un traguardo o una funzionalità specifica"],
     "cta": "Aiuta economicamente →", "subject": "Sostenere economicamente OpenElpis"},
    {"ic": "plus", "h": "Altri modi per aiutare",
     "p": "Non rientri in nessuna delle caselle qui sopra? C'è comunque molto che puoi fare.",
     "ul": ["Contribuisci al codice open-source su GitHub",
            "Presentaci a un programma di crediti cloud / per non-profit o a un possibile finanziatore",
            "Offri aiuto pro-bono (legale, design, costituzione dell'ente, traduzione)",
            "Semplicemente, fai girare la voce"],
     "cta": "Mettiti in contatto →", "subject": "Aiutare OpenElpis"},
  ],
  "returns_head": "Cosa ricevono i partner in cambio",
  "returns": ["Riconoscimento come partner fondatore su openelpis.com, sul nostro GitHub e nelle note di rilascio",
              "Un vero caso di studio d'impatto — un articolo di blog co-firmato sul sostegno alla ricerca aperta sul cancro al seno",
              "Report d'impatto periodici: contributori inseriti, dimensione del corpus, query di ricercatori gestite, set di dati pubblicati",
              "Co-marketing facoltativo attorno ai traguardi, totalmente a tua discrezione"],
  "risk_head": "Perché sostenere OpenElpis è a basso rischio",
  "risk": ["Nessun dato grezzo dei pazienti per progettazione nelle prime fasi — solo letteratura pubblicata e materiale anonimizzato e aggregato",
           "Validato da esseri umani, verificabile e ritirabile — non raccolto in tempo reale e mai addestrato direttamente sui caricamenti",
           "Open-source e portabile tra fornitori — nessun vincolo proprietario",
           "Quadro di responsabilità chiaro: uno strumento di supporto alla ricerca per professionisti, mai diagnosi o consigli terapeutici"],
  "final_h": "Pronto ad aiutare? Parliamone.",
  "final_p": "Dicci come vorresti sostenere OpenElpis — infrastruttura, cooperazione clinica, ricerca o finanziamento — e pensiamo a tutto noi.",
  "final_email": "Scrivi a hello@openelpis.com", "final_pdf": "Scarica la proposta (PDF)",
  "home": "← Torna a OpenElpis", "crumb": "OpenElpis",
 },
 "ru": {
  "title": "Как вы можете помочь — Станьте партнёром OpenElpis",
  "desc": "Конкретные способы поддержать OpenElpis — некоммерческий ИИ-помощник с открытым исходным кодом для исследований рака молочной железы: пожертвуйте облако, вычисления или серверы; сотрудничайте как клиника или больница; участвуйте как исследователь; или помогите финансово.",
  "badge": "Станьте партнёром OpenElpis", "h1": "Как вы можете помочь",
  "lead": "OpenElpis работает на пожертвованной инфраструктуре и доброй воле. Это некоммерческий проект с открытым исходным кодом — пока не зарегистрированное юридическое лицо — который создаёт надёжного, основанного на источниках ИИ-помощника для исследований рака молочной железы, бесплатного для всех. Вот конкретные способы, которыми вы можете помочь воплотить это в жизнь.",
  "pdf": "Прочитать полное предложение (PDF)", "email": "Напишите нам",
  "ways_head": "Способы помочь",
  "ways": [
    {"ic": "server", "h": "Поставщики облака, вычислений и инфраструктуры",
     "p": "Если ваша компания предоставляет облако, хостинг, хранилище, CDN или GPU-вычисления, пожертвованные кредиты или бесплатные тарифы напрямую питают платформу. Наши потребности на Фазе 1 невелики и переносимы (~3 000–5 000 USD/год по рыночной стоимости) — покрытие даже одной строки существенно снижает риски проекта.",
     "ul": ["Вычислительный сервер, работающий 24/7 (~8 vCPU / 16–32 ГБ RAM / 200 ГБ NVMe)",
            "Объектное хранилище (250 ГБ – 2 ТБ, совместимое с S3)",
            "CDN + WAF + защита от DDoS перед публичным сайтом",
            "Управляемый PostgreSQL (~2 vCPU / 8 ГБ / 100 ГБ)",
            "Эпизодические GPU-вычисления (несколько сотен GPU-часов класса A100/H100 в год)"],
     "cta": "Предоставить инфраструктуру →", "subject": "Поддержка инфраструктуры для OpenElpis"},
    {"ic": "clinic", "h": "Врачи, клиники и больницы",
     "p": "Клинические партнёры — это сердце цепочки доверия. Вы можете помочь проверять то, что попадает в систему, и направлять то, что мы создаём, — без использования каких-либо данных, позволяющих идентифицировать пациента, на ранних этапах.",
     "ul": ["Станьте проверенным участником или экспертом-рецензентом литературы и данных по раку молочной железы",
            "Делитесь обезличенными, агрегированными данными через управляемый, этически приоритетный процесс (откладывается до появления соглашений об использовании данных и надзора IRB/этики)",
            "Консультируйте по клинической значимости, безопасности и действительно важным исследовательским вопросам",
            "Помогите сформировать стандарты управления данными и рецензирования"],
     "cta": "Сотрудничать клинически →", "subject": "Клиническое сотрудничество с OpenElpis"},
    {"ic": "flask", "h": "Исследователи и академические учреждения",
     "p": "Исследователи и лаборатории делают корпус достойным доверия.",
     "ul": ["Предоставляйте проверенную литературу, обезличенные наборы данных или структурированные результаты",
            "Проводите рецензирование материалов и консультируйте по курируемому корпусу",
            "Сотрудничайте над методами, оценкой и открытыми публикациями",
            "Свяжите нас с подходящими программами, грантами или сообществами"],
     "cta": "Сотрудничать →", "subject": "Научное сотрудничество с OpenElpis"},
    {"ic": "heart", "h": "Частные лица, желающие помочь финансово",
     "p": "OpenElpis — независимый некоммерческий проект, который пока не является зарегистрированным юридическим лицом, поэтому публичной кнопки пожертвования пока нет. Если вы хотите поддержать его финансово, свяжитесь с нами — каждое предложение рассматривается открыто и прозрачно.",
     "ul": ["Возьмите на себя покрытие конкретных расходов (например, месяц работы сервера или хранилища)",
            "Зарегистрируйте своё намерение сделать пожертвование, когда появится зарегистрированная некоммерческая организация / фискальный спонсор",
            "Поддержите этап или конкретную функцию"],
     "cta": "Помочь финансово →", "subject": "Финансовая поддержка OpenElpis"},
    {"ic": "plus", "h": "Другие способы помочь",
     "p": "Не подходите ни под один из пунктов выше? Вы всё равно можете многое сделать.",
     "ul": ["Внесите вклад в открытый исходный код на GitHub",
            "Познакомьте нас с программой облачных/некоммерческих кредитов или с потенциальным спонсором",
            "Предложите помощь pro bono (юридическую, дизайн, регистрацию организации, перевод)",
            "Просто расскажите о нас"],
     "cta": "Связаться →", "subject": "Помощь OpenElpis"},
  ],
  "returns_head": "Что партнёры получают взамен",
  "returns": ["Признание в качестве партнёра-основателя на openelpis.com, в нашем GitHub и в примечаниях к выпускам",
              "Реальная история воздействия — совместно написанная статья в блоге о поддержке открытых исследований рака молочной железы",
              "Регулярные отчёты о воздействии: привлечённые участники, размер корпуса, обработанные запросы исследователей, опубликованные наборы данных",
              "Опциональный совместный маркетинг вокруг ключевых этапов, полностью на ваше усмотрение"],
  "risk_head": "Почему поддержка OpenElpis сопряжена с низким риском",
  "risk": ["На ранних этапах по замыслу нет необработанных данных пациентов — только опубликованная литература и обезличенный, агрегированный материал",
           "Проверено людьми, поддаётся аудиту и отзыву — не собирается вживую и никогда не обучается напрямую на загрузках",
           "Открытый исходный код и переносимость между поставщиками — без привязки к проприетарным решениям",
           "Чёткие рамки ответственности: инструмент поддержки исследований для специалистов, никогда не диагноз и не рекомендации по лечению"],
  "final_h": "Готовы помочь? Давайте обсудим.",
  "final_p": "Расскажите, как вы хотели бы поддержать OpenElpis — инфраструктура, клиническое сотрудничество, исследования или финансирование — а остальное мы возьмём на себя.",
  "final_email": "Написать на hello@openelpis.com", "final_pdf": "Скачать предложение (PDF)",
  "home": "← Назад в OpenElpis", "crumb": "OpenElpis",
 },
 "nl": {
  "title": "Hoe u kunt helpen — Word partner van OpenElpis",
  "desc": "Concrete manieren om OpenElpis te steunen, een non-profit, open-source AI-onderzoekscopiloot voor borstkanker: doneer cloud, rekenkracht of servers; werk samen als kliniek of ziekenhuis; werk mee als onderzoeker; of help financieel.",
  "badge": "Word partner van OpenElpis", "h1": "Hoe u kunt helpen",
  "lead": "OpenElpis draait op gedoneerde infrastructuur en goodwill. Het is een non-profit, open-source project — nog geen geregistreerde rechtspersoon — dat een betrouwbare, op citaten gebaseerde AI-onderzoekscopiloot voor borstkanker bouwt, gratis voor iedereen. Dit zijn de concrete manieren waarop u kunt helpen dit waar te maken.",
  "pdf": "Lees het volledige voorstel (PDF)", "email": "Mail ons",
  "ways_head": "Manieren om te helpen",
  "ways": [
    {"ic": "server", "h": "Aanbieders van cloud, rekenkracht en infrastructuur",
     "p": "Als uw bedrijf cloud, hosting, opslag, CDN of GPU-rekenkracht levert, voeden gedoneerde credits of gratis capaciteit het platform rechtstreeks. Onze Fase 1-voetafdruk is klein en overdraagbaar (~$3.000–$5.000/jaar aan marktwaarde) — zelfs één regel dekken verlaagt het projectrisico aanzienlijk.",
     "ul": ["Een 24/7 rekeninstantie (~8 vCPU / 16–32 GB RAM / 200 GB NVMe)",
            "Objectopslag (250 GB – 2 TB, S3-compatibel)",
            "CDN + WAF + DDoS-bescherming vóór de openbare site",
            "Beheerde PostgreSQL (~2 vCPU / 8 GB / 100 GB)",
            "Incidentele GPU-rekenkracht (een paar honderd GPU-uren van A100/H100-klasse per jaar)"],
     "cta": "Infrastructuur aanbieden →", "subject": "Infrastructuurondersteuning voor OpenElpis"},
    {"ic": "clinic", "h": "Artsen, klinieken en ziekenhuizen",
     "p": "Klinische partners vormen het hart van de vertrouwenspijplijn. U kunt helpen valideren wat erin gaat en mee sturen wat we bouwen — zonder dat er in de vroege fasen patiëntidentificeerbare gegevens worden gebruikt.",
     "ul": ["Word geverifieerde bijdrager of deskundige beoordelaar van borstkankerliteratuur en -bevindingen",
            "Deel geanonimiseerde, geaggregeerde bevindingen via een beheerde, ethiek-eerst pijplijn (uitgesteld tot er gegevensgebruiksovereenkomsten en IRB-/ethisch toezicht zijn)",
            "Adviseer over klinische relevantie, veiligheid en de echte onderzoeksvragen die het waard zijn te beantwoorden",
            "Help de normen voor datagovernance en beoordeling vorm te geven"],
     "cta": "Klinisch samenwerken →", "subject": "Klinische samenwerking met OpenElpis"},
    {"ic": "flask", "h": "Onderzoekers en academische instellingen",
     "p": "Onderzoekers en labs maken het corpus het vertrouwen waard.",
     "ul": ["Draag gevalideerde literatuur, geanonimiseerde datasets of gestructureerde bevindingen bij",
            "Beoordeel inzendingen via peer review en adviseer over het samengestelde corpus",
            "Werk samen aan methoden, evaluatie en open publicaties",
            "Breng ons in contact met relevante programma's, subsidies of gemeenschappen"],
     "cta": "Samenwerken →", "subject": "Onderzoekssamenwerking met OpenElpis"},
    {"ic": "heart", "h": "Particulieren die financieel willen helpen",
     "p": "OpenElpis is een onafhankelijk, non-profit project en is nog geen geregistreerde rechtspersoon, dus er is nog geen openbare donatieknop. Als u het financieel wilt steunen, neem dan contact op — elk aanbod wordt open en transparant behandeld.",
     "ul": ["Zeg toe een specifieke lopende kostenpost te dekken (bijv. een maand server of opslag)",
            "Registreer uw interesse om te doneren zodra er een geregistreerde non-profit / fiscale sponsor is",
            "Sponsor een mijlpaal of een specifieke functie"],
     "cta": "Financieel helpen →", "subject": "OpenElpis financieel steunen"},
    {"ic": "plus", "h": "Andere manieren om te helpen",
     "p": "Past u niet in een van de bovenstaande vakjes? Er is nog steeds veel dat u kunt doen.",
     "ul": ["Draag bij aan de open-source code op GitHub",
            "Stel ons voor aan een cloud-/non-profit-creditprogramma of een mogelijke financier",
            "Bied pro-bono hulp aan (juridisch, ontwerp, oprichting van de organisatie, vertaling)",
            "Vertel het gewoon verder"],
     "cta": "Neem contact op →", "subject": "OpenElpis helpen"},
  ],
  "returns_head": "Wat partners in ruil ontvangen",
  "returns": ["Erkenning als oprichtende partner op openelpis.com, onze GitHub en de release-notes",
              "Een echt impactverhaal — een samen geschreven blogpost over het mogelijk maken van open borstkankeronderzoek",
              "Periodieke impactrapporten: aangesloten bijdragers, corpusgrootte, beantwoorde onderzoekersvragen, gepubliceerde datasets",
              "Optionele co-marketing rond mijlpalen, geheel naar eigen goeddunken"],
  "risk_head": "Waarom OpenElpis steunen weinig risico inhoudt",
  "risk": ["Geen ruwe patiëntgegevens door ontwerp in de vroege fasen — alleen gepubliceerde literatuur en geanonimiseerd, geaggregeerd materiaal",
           "Door mensen gevalideerd, controleerbaar en intrekbaar — niet live gescrapet en nooit rechtstreeks op uploads getraind",
           "Open-source en overdraagbaar tussen aanbieders — geen propriëtaire lock-in",
           "Duidelijk aansprakelijkheidskader: een onderzoeksondersteunend hulpmiddel voor professionals, nooit diagnose of behandeladvies"],
  "final_h": "Klaar om te helpen? Laten we praten.",
  "final_p": "Vertel ons hoe u OpenElpis wilt steunen — infrastructuur, klinische samenwerking, onderzoek of financiering — en wij doen de rest.",
  "final_email": "Mail naar hello@openelpis.com", "final_pdf": "Download het voorstel (PDF)",
  "home": "← Terug naar OpenElpis", "crumb": "OpenElpis",
 },
}

SPONSOR_CSS = """<style>
  .badge{display:inline-flex;gap:8px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:999px;padding:6px 14px;font-size:13px;font-weight:600;color:var(--rose-deep);box-shadow:var(--shadow)}
  .badge .dot{width:7px;height:7px;border-radius:50%;background:var(--rose)}
  .s-hero .wrap{padding:56px 24px 44px;max-width:860px}
  .crumb{font-size:13px;color:var(--muted);margin-bottom:20px}.crumb a{color:var(--muted)}
  h1.s-title{font-family:'Sora',-apple-system,sans-serif;font-weight:800;letter-spacing:-.03em;line-height:1.04;font-size:clamp(34px,5.4vw,54px);margin:16px 0 0}
  .s-lead{font-size:clamp(17px,2vw,20px);color:var(--ink-soft);max-width:62ch;margin-top:18px}
  .s-cta{display:flex;flex-wrap:wrap;gap:13px;margin-top:26px}
  .btn{display:inline-flex;align-items:center;gap:8px;font-weight:600;font-size:15px;padding:12px 22px;border-radius:999px;border:1px solid transparent;transition:.18s;cursor:pointer}
  .btn-primary{background:var(--rose-dark);color:#fff!important;box-shadow:0 8px 20px -8px rgba(168,47,95,.5)}
  .btn-primary:hover{background:#8a2750;transform:translateY(-1px)}
  .btn-ghost{background:#fff;color:var(--ink)!important;border-color:var(--line)}
  .btn-ghost:hover{border-color:var(--teal);color:var(--teal-deep)!important}
  .btn-light{background:#fff;color:var(--teal-deep)!important;border-color:var(--line)}
  .btn-line{background:transparent;color:#fff!important;border-color:rgba(255,255,255,.45)}
  .btn-line:hover{border-color:#fff}
  section.ways{padding:24px 0 8px}
  .sec-head{max-width:60ch}.sec-head h2{font-size:clamp(26px,3.6vw,38px)}
  .ways-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:22px;margin-top:30px}
  .way{background:#fff;border:1px solid var(--line);border-radius:18px;padding:28px;box-shadow:var(--shadow);display:flex;flex-direction:column}
  .way .ic{width:46px;height:46px;border-radius:12px;display:grid;place-items:center;background:var(--mint);color:var(--teal-deep);margin-bottom:16px}
  .way:nth-child(2) .ic{background:var(--rose-soft);color:var(--rose-deep)}
  .way:nth-child(4) .ic{background:var(--rose-soft);color:var(--rose-deep)}
  .way h3{font-size:20px;margin-bottom:8px}
  .way>p{color:var(--ink-soft);font-size:15.5px;margin-bottom:15px}
  .way ul{list-style:none;margin:0 0 18px;padding:0;display:flex;flex-direction:column;gap:9px}
  .way li{position:relative;padding-left:22px;font-size:14.5px;color:var(--ink-soft);line-height:1.5}
  .way li::before{content:"";position:absolute;left:0;top:8px;width:8px;height:8px;border-radius:50%;background:var(--teal)}
  .way .cta{margin-top:auto;align-self:flex-start;font-weight:600;font-size:14px;color:var(--rose-dark)}
  .way .cta:hover{color:#8a2750}
  section.feats{padding:48px 0}
  .two-col{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:26px}
  .feat{background:#fff;border:1px solid var(--line);border-radius:16px;padding:26px;box-shadow:var(--shadow)}
  .feat h3{font-size:18px;margin-bottom:14px}
  .feat ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:11px}
  .feat li{position:relative;padding-left:26px;font-size:15px;color:var(--ink-soft);line-height:1.5}
  .feat li::before{content:"✓";position:absolute;left:0;top:0;color:var(--teal-deep);font-weight:700}
  .cta-band{background:linear-gradient(135deg,var(--teal-deep),var(--teal-dark));color:#fff;border-radius:26px;padding:50px 40px;text-align:center;margin:8px 0 52px;box-shadow:0 30px 60px -30px rgba(7,63,62,.6)}
  .cta-band h2{color:#fff;font-size:clamp(24px,3.4vw,34px)}
  .cta-band p{color:rgba(255,255,255,.85);margin:14px auto 0;max-width:54ch}
  .cta-band .s-cta{justify-content:center}
  @media(max-width:760px){.two-col{grid-template-columns:1fr}}
</style>"""

def sponsor_html(lang):
    c = SPONSOR[lang]
    url = BASE + sponsor_url(lang)
    home = "/" if lang == "en" else f"/{lang}/"
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": c["title"],
          "description": c["desc"], "url": url, "inLanguage": lang,
          "isPartOf": {"@type": "WebSite", "name": "OpenElpis", "url": f"{BASE}/"},
          "about": {"@type": "Organization", "name": "OpenElpis", "url": f"{BASE}/"}}
    crumb = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "OpenElpis", "item": f"{BASE}/"},
        {"@type": "ListItem", "position": 2, "name": c["h1"], "item": url}]}
    hl = "\n".join(
        [f'<link rel="alternate" hreflang="{l}" href="{BASE}{sponsor_url(l)}">' for l in SPONSOR_LANGS]
        + [f'<link rel="alternate" hreflang="x-default" href="{BASE}{sponsor_url("en")}">'])

    ways = ""
    for w in c["ways"]:
        lis = "".join(f"<li>{esc_text(x)}</li>" for x in w["ul"])
        ways += (f'<div class="way"><div class="ic">{IC[w["ic"]]}</div>'
                 f'<h3>{esc_text(w["h"])}</h3><p>{esc_text(w["p"])}</p><ul>{lis}</ul>'
                 f'<a class="cta" href="{mailto(w["subject"])}">{esc_text(w["cta"])}</a></div>\n')
    returns = "".join(f"<li>{esc_text(x)}</li>" for x in c["returns"])
    risk = "".join(f"<li>{esc_text(x)}</li>" for x in c["risk"])

    head = (
      f'<!DOCTYPE html>\n<html lang="{lang}">\n<head>\n'
      f'<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
      f'<title>{esc_text(c["title"])}</title>\n<meta name="description" content="{esc_attr(c["desc"])}">\n'
      f'<link rel="canonical" href="{url}">\n<meta name="robots" content="index, follow, max-image-preview:large">\n{hl}\n'
      f'<meta property="og:type" content="website">\n<meta property="og:title" content="{esc_attr(c["title"])}">\n'
      f'<meta property="og:description" content="{esc_attr(c["desc"])}">\n'
      f'<meta property="og:image" content="{BASE}/logo.png">\n<meta property="og:url" content="{url}">\n'
      f'<meta property="og:site_name" content="OpenElpis">\n<meta property="og:locale" content="{LOCALE[lang]}">\n'
      f'<meta name="twitter:card" content="summary_large_image">\n<meta name="twitter:title" content="{esc_attr(c["title"])}">\n'
      f'<meta name="twitter:description" content="{esc_attr(c["desc"])}">\n<meta name="twitter:image" content="{BASE}/logo.png">\n'
      f'<meta name="theme-color" content="#0a5b5a">\n'
      f'<link rel="preconnect" href="https://fonts.googleapis.com">\n<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
      f'<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,ital,wght@9..144,1,400;9..144,0,400;9..144,0,500;9..144,0,600&family=Inter:wght@400;500;600;700&family=Sora:wght@600;700;800&display=swap" rel="stylesheet">\n'
      f'<link rel="icon" type="image/svg+xml" href="/logo.svg">\n'
      f'<script type="application/ld+json">\n{json.dumps(ld, ensure_ascii=False, indent=2)}\n</script>\n'
      f'<script type="application/ld+json">\n{json.dumps(crumb, ensure_ascii=False, indent=2)}\n</script>\n'
      f'{STYLE}\n{SPONSOR_CSS}\n</head>\n')

    body = (
      f'<body>\n'
      f'<header class="nav"><div class="wrap">'
      f'<a class="brand" href="{home}"><img class="mark" src="/logo.svg" alt="OpenElpis" width="36" height="36"> OpenElpis</a>'
      f'<a class="home" href="mailto:hello@openelpis.com">{esc_text(c["email"])}</a></div></header>\n'
      f'<section class="hero s-hero"><div class="wrap">\n'
      f'<nav class="crumb" aria-label="Breadcrumb"><a href="{home}">{esc_text(c["crumb"])}</a> &nbsp;›&nbsp; {esc_text(c["h1"])}</nav>\n'
      f'<span class="badge"><span class="dot"></span> {esc_text(c["badge"])}</span>\n'
      f'<h1 class="s-title">{esc_text(c["h1"])}</h1>\n<p class="s-lead">{esc_text(c["lead"])}</p>\n'
      f'<div class="s-cta">'
      f'<a class="btn btn-primary" href="/OpenElpis-Proposal.pdf" target="_blank" rel="noopener">{esc_text(c["pdf"])}</a>'
      f'<a class="btn btn-ghost" href="mailto:hello@openelpis.com">{esc_text(c["email"])}</a>'
      f'</div>\n</div></section>\n'
      f'<section class="ways"><div class="wrap">\n'
      f'<div class="sec-head"><span class="eyebrow">{esc_text(c["ways_head"])}</span></div>\n'
      f'<div class="ways-grid">\n{ways}</div>\n</div></section>\n'
      f'<section class="feats"><div class="wrap">\n<div class="two-col">\n'
      f'<div class="feat"><h3>{esc_text(c["returns_head"])}</h3><ul>{returns}</ul></div>\n'
      f'<div class="feat"><h3>{esc_text(c["risk_head"])}</h3><ul>{risk}</ul></div>\n'
      f'</div></div></section>\n'
      f'<section style="padding:0 0 8px"><div class="wrap"><div class="cta-band">\n'
      f'<h2>{esc_text(c["final_h"])}</h2><p>{esc_text(c["final_p"])}</p>\n'
      f'<div class="s-cta">'
      f'<a class="btn btn-light" href="mailto:hello@openelpis.com">{esc_text(c["final_email"])}</a>'
      f'<a class="btn btn-line" href="/OpenElpis-Proposal.pdf" target="_blank" rel="noopener">{esc_text(c["final_pdf"])}</a>'
      f'<a class="btn btn-line" href="{home}">{esc_text(c["home"])}</a>'
      f'</div>\n</div></div></section>\n'
      f'<footer style="background:#07302f;color:#cfe1de;padding:42px 0 36px"><div class="wrap" style="display:flex;flex-wrap:wrap;gap:16px;justify-content:space-between;align-items:center">'
      f'<a class="brand" href="{home}" style="color:#fff"><img class="mark" src="/logo.svg" alt="OpenElpis" width="36" height="36"> OpenElpis</a>'
      f'<a href="mailto:hello@openelpis.com" style="color:#9fc7c2">hello@openelpis.com</a>'
      f'</div><div class="wrap"><p style="font-size:13px;color:#7fa9a4;max-width:74ch;margin-top:14px">'
      f'{esc_text(tr_all[lang]["footer_entity"] if lang in tr_all else c["lead"])}</p></div></footer>\n'
      f'</body>\n</html>\n')
    return head + body

for lang in SPONSOR_LANGS:
    d = "sponsor" if lang == "en" else f"{lang}/sponsor"
    os.makedirs(d, exist_ok=True)
    open(f"{d}/index.html", "w", encoding="utf-8").write(sponsor_html(lang))
    print(f"wrote {d}/index.html")

# ───────────────────────── 3. sitemap.xml ─────────────────────
def hreflang_links(pairs, indent="    "):
    rows = [f'{indent}<xhtml:link rel="alternate" hreflang="{l}" href="{u}"/>' for l, u in pairs]
    rows.append(f'{indent}<xhtml:link rel="alternate" hreflang="x-default" href="{pairs[0][1]}"/>')
    return "\n".join(rows)

img = (f'    <image:image>\n'
       f'      <image:loc>{BASE}/ali-tajbakhsh.jpg</image:loc>\n'
       f'      <image:title>Ali Tajbakhsh — founder of OpenElpis</image:title>\n'
       f'      <image:caption>Ali Tajbakhsh (also known as Mohammadali Tajbakhsh and Alan Tajbakhsh), founder of OpenElpis</image:caption>\n'
       f'    </image:image>')

def url_entry(loc, pairs, prio, freq, with_img=False):
    extra = "\n" + img if with_img else ""
    return (f'  <url>\n    <loc>{loc}</loc>\n    <lastmod>{LASTMOD}</lastmod>\n'
            f'    <changefreq>{freq}</changefreq>\n    <priority>{prio}</priority>\n'
            f'{hreflang_links(pairs)}{extra}\n  </url>')

urls = []
home_pairs = [(l, f"{BASE}{URL[l]}") for l in ALL]
for l in ALL:
    urls.append(url_entry(f"{BASE}{URL[l]}", home_pairs, "1.0" if l == "en" else "0.8", "weekly", with_img=(l == "en")))
# founder page (single-language)
urls.append(f'  <url>\n    <loc>{BASE}/founder/</loc>\n    <lastmod>{LASTMOD}</lastmod>\n'
            f'    <changefreq>monthly</changefreq>\n    <priority>0.9</priority>\n{img}\n  </url>')
# name profiles (en + tr)
for p in PROFILES:
    pairs = [(l, f"{BASE}{prof_url(p['slug'], l)}") for l in PROF_LANGS]
    for l in PROF_LANGS:
        urls.append(url_entry(f"{BASE}{prof_url(p['slug'], l)}", pairs, "0.7", "monthly", with_img=(l == "en")))
# sponsor / how-to-help page (en + tr)
sponsor_pairs = [(l, f"{BASE}{sponsor_url(l)}") for l in SPONSOR_LANGS]
for l in SPONSOR_LANGS:
    urls.append(url_entry(f"{BASE}{sponsor_url(l)}", sponsor_pairs, "0.9", "monthly"))

sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
           '        xmlns:xhtml="http://www.w3.org/1999/xhtml"\n'
           '        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n'
           + "\n".join(urls) + "\n</urlset>\n")
open("sitemap.xml", "w", encoding="utf-8").write(sitemap)
print(f"wrote sitemap.xml ({len(urls)} urls)")
