"""
Transactional email via the Brevo SMTP relay (smtp-relay.brevo.com:587, STARTTLS).

Best-effort by design: callers use `try_send_invite(...)`, which NEVER raises — if
email isn't configured or the send fails, the invite is still created and its link is
returned in the API response for manual copy. Credentials come from /etc/openelpis.env
(never the repo): BREVO_SMTP_LOGIN (the Brevo SMTP relay login)
+ BREVO_SMTP_KEY (the `xsmtpsib-…` SMTP key). The sender domain openelpis.com is
Brevo-verified (SPF include:spf.brevo.com + DKIM mail._domainkey + DMARC), so From hello@
is aligned.

Invite emails are localized to the INVITER's language (passed from the dashboard, which
knows the inviter's stored UI language) across all 8 site languages; access-request
approvals default to English (the recipient's language isn't known).
"""
import os, smtplib, ssl, logging
from html import escape as _esc
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger("openelpis.mailer")

SMTP_HOST      = os.environ.get("BREVO_SMTP_HOST", "smtp-relay.brevo.com")
SMTP_PORT      = int(os.environ.get("BREVO_SMTP_PORT", "587"))
SMTP_LOGIN     = os.environ.get("BREVO_SMTP_LOGIN", "")
SMTP_KEY       = os.environ.get("BREVO_SMTP_KEY", "")
MAIL_FROM      = os.environ.get("MAIL_FROM", "hello@openelpis.com")
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "OpenElpis")
SITE           = os.environ.get("SITE_ORIGIN", "https://openelpis.com")

# Per-language copy. {inviter} = the inviting member's name; {expires} = a date string.
LOCALES = {
    "en": {
        "subj_inv": "{inviter} is inviting you to OpenElpis — a breast cancer research copilot",
        "subj_app": "Your OpenElpis access request was approved",
        "head_inv": "You're invited", "head_app": "You're approved",
        "intro_inv": "<b>{inviter}</b> has invited you to join <b>OpenElpis</b>.",
        "intro_app": "Good news — your request to join <b>OpenElpis</b> has been approved.",
        "what": "OpenElpis is a non-profit, citation-grounded AI research copilot for breast cancer — an invitation-only community where verified clinicians, hospitals and labs contribute validated knowledge and explore it together.",
        "cta": "Create your account", "expiry": "This invitation is valid for <b>14 days</b> — until {expires}.",
        "fallback": "If the button doesn't work, paste this link into your browser:",
        "more": "Learn more at", "disc": "OpenElpis produces research hypotheses for qualified professionals — never diagnosis or treatment advice. If you weren't expecting this email, you can safely ignore it.",
    },
    "tr": {
        "subj_inv": "{inviter} sizi OpenElpis'e davet ediyor — meme kanseri araştırma asistanı",
        "subj_app": "OpenElpis erişim talebiniz onaylandı",
        "head_inv": "Davet edildiniz", "head_app": "Onaylandınız",
        "intro_inv": "<b>{inviter}</b> sizi <b>OpenElpis</b>'e katılmaya davet ediyor.",
        "intro_app": "Güzel haber — <b>OpenElpis</b>'e katılma talebiniz onaylandı.",
        "what": "OpenElpis, meme kanseri için kâr amacı gütmeyen, kaynak gösteren bir yapay zekâ araştırma asistanıdır — doğrulanmış klinisyenlerin, hastanelerin ve laboratuvarların doğrulanmış bilgi katkısında bulunup birlikte incelediği, yalnızca davetle katılınan bir topluluktur.",
        "cta": "Hesabınızı oluşturun", "expiry": "Bu davet <b>14 gün</b> geçerlidir — {expires} tarihine kadar.",
        "fallback": "Düğme çalışmazsa bu bağlantıyı tarayıcınıza yapıştırın:",
        "more": "Daha fazla bilgi:", "disc": "OpenElpis, nitelikli profesyoneller için araştırma hipotezleri üretir — asla teşhis veya tedavi önerisi sunmaz. Bu e-postayı beklemiyorduysanız görmezden gelebilirsiniz.",
    },
    "es": {
        "subj_inv": "{inviter} te invita a OpenElpis — un copiloto de investigación sobre el cáncer de mama",
        "subj_app": "Tu solicitud de acceso a OpenElpis fue aprobada",
        "head_inv": "Estás invitado", "head_app": "Has sido aprobado",
        "intro_inv": "<b>{inviter}</b> te ha invitado a unirte a <b>OpenElpis</b>.",
        "intro_app": "Buenas noticias: tu solicitud para unirte a <b>OpenElpis</b> ha sido aprobada.",
        "what": "OpenElpis es un copiloto de investigación con IA, sin ánimo de lucro y basado en citas, para el cáncer de mama: una comunidad solo por invitación donde clínicos, hospitales y laboratorios verificados aportan conocimiento validado y lo exploran juntos.",
        "cta": "Crea tu cuenta", "expiry": "Esta invitación es válida durante <b>14 días</b>, hasta el {expires}.",
        "fallback": "Si el botón no funciona, pega este enlace en tu navegador:",
        "more": "Más información en", "disc": "OpenElpis produce hipótesis de investigación para profesionales cualificados, nunca diagnósticos ni recomendaciones de tratamiento. Si no esperabas este correo, puedes ignorarlo.",
    },
    "de": {
        "subj_inv": "{inviter} lädt Sie zu OpenElpis ein — einem Forschungs-Copiloten für Brustkrebs",
        "subj_app": "Ihre OpenElpis-Zugangsanfrage wurde genehmigt",
        "head_inv": "Sie sind eingeladen", "head_app": "Sie sind freigeschaltet",
        "intro_inv": "<b>{inviter}</b> hat Sie eingeladen, <b>OpenElpis</b> beizutreten.",
        "intro_app": "Gute Nachrichten — Ihre Anfrage, <b>OpenElpis</b> beizutreten, wurde genehmigt.",
        "what": "OpenElpis ist ein gemeinnütziger, zitatbasierter KI-Forschungs-Copilot für Brustkrebs — eine Community nur mit Einladung, in der verifizierte Fachpersonen, Krankenhäuser und Labore validiertes Wissen beitragen und gemeinsam erkunden.",
        "cta": "Konto erstellen", "expiry": "Diese Einladung ist <b>14 Tage</b> gültig — bis zum {expires}.",
        "fallback": "Falls die Schaltfläche nicht funktioniert, fügen Sie diesen Link in Ihren Browser ein:",
        "more": "Mehr erfahren auf", "disc": "OpenElpis erstellt Forschungshypothesen für qualifizierte Fachleute — niemals Diagnosen oder Behandlungsempfehlungen. Falls Sie diese E-Mail nicht erwartet haben, können Sie sie ignorieren.",
    },
    "fr": {
        "subj_inv": "{inviter} vous invite sur OpenElpis — un copilote de recherche sur le cancer du sein",
        "subj_app": "Votre demande d'accès à OpenElpis a été approuvée",
        "head_inv": "Vous êtes invité", "head_app": "Vous êtes approuvé",
        "intro_inv": "<b>{inviter}</b> vous a invité à rejoindre <b>OpenElpis</b>.",
        "intro_app": "Bonne nouvelle — votre demande pour rejoindre <b>OpenElpis</b> a été approuvée.",
        "what": "OpenElpis est un copilote de recherche par IA, à but non lucratif et fondé sur des citations, pour le cancer du sein : une communauté sur invitation où des cliniciens, hôpitaux et laboratoires vérifiés contribuent un savoir validé et l'explorent ensemble.",
        "cta": "Créer votre compte", "expiry": "Cette invitation est valable <b>14 jours</b> — jusqu'au {expires}.",
        "fallback": "Si le bouton ne fonctionne pas, collez ce lien dans votre navigateur :",
        "more": "En savoir plus sur", "disc": "OpenElpis produit des hypothèses de recherche pour des professionnels qualifiés — jamais de diagnostic ni de conseil thérapeutique. Si vous n'attendiez pas cet e-mail, vous pouvez l'ignorer.",
    },
    "it": {
        "subj_inv": "{inviter} ti invita su OpenElpis — un copilota di ricerca sul cancro al seno",
        "subj_app": "La tua richiesta di accesso a OpenElpis è stata approvata",
        "head_inv": "Sei invitato", "head_app": "Sei approvato",
        "intro_inv": "<b>{inviter}</b> ti ha invitato a unirti a <b>OpenElpis</b>.",
        "intro_app": "Buone notizie — la tua richiesta di unirti a <b>OpenElpis</b> è stata approvata.",
        "what": "OpenElpis è un copilota di ricerca con IA, senza scopo di lucro e basato su citazioni, per il cancro al seno: una comunità su invito in cui clinici, ospedali e laboratori verificati contribuiscono conoscenza validata e la esplorano insieme.",
        "cta": "Crea il tuo account", "expiry": "Questo invito è valido per <b>14 giorni</b> — fino al {expires}.",
        "fallback": "Se il pulsante non funziona, incolla questo link nel browser:",
        "more": "Scopri di più su", "disc": "OpenElpis produce ipotesi di ricerca per professionisti qualificati — mai diagnosi o consigli terapeutici. Se non aspettavi questa email, puoi ignorarla.",
    },
    "ru": {
        "subj_inv": "{inviter} приглашает вас в OpenElpis — исследовательский ассистент по раку молочной железы",
        "subj_app": "Ваш запрос на доступ к OpenElpis одобрен",
        "head_inv": "Вас пригласили", "head_app": "Вы одобрены",
        "intro_inv": "<b>{inviter}</b> приглашает вас присоединиться к <b>OpenElpis</b>.",
        "intro_app": "Хорошие новости — ваш запрос на присоединение к <b>OpenElpis</b> одобрен.",
        "what": "OpenElpis — некоммерческий исследовательский ИИ-ассистент с цитированием источников по раку молочной железы: сообщество только по приглашениям, где проверенные клиницисты, больницы и лаборатории вносят проверенные знания и изучают их вместе.",
        "cta": "Создать аккаунт", "expiry": "Это приглашение действительно <b>14 дней</b> — до {expires}.",
        "fallback": "Если кнопка не работает, вставьте эту ссылку в браузер:",
        "more": "Подробнее:", "disc": "OpenElpis формирует исследовательские гипотезы для квалифицированных специалистов — никогда не ставит диагноз и не назначает лечение. Если вы не ожидали это письмо, можете его проигнорировать.",
    },
    "nl": {
        "subj_inv": "{inviter} nodigt u uit voor OpenElpis — een borstkanker-onderzoekscopilot",
        "subj_app": "Uw toegangsverzoek voor OpenElpis is goedgekeurd",
        "head_inv": "U bent uitgenodigd", "head_app": "U bent goedgekeurd",
        "intro_inv": "<b>{inviter}</b> heeft u uitgenodigd om lid te worden van <b>OpenElpis</b>.",
        "intro_app": "Goed nieuws — uw verzoek om lid te worden van <b>OpenElpis</b> is goedgekeurd.",
        "what": "OpenElpis is een non-profit, op citaten gebaseerde AI-onderzoekscopilot voor borstkanker: een community op uitnodiging waar geverifieerde clinici, ziekenhuizen en labs gevalideerde kennis bijdragen en die samen verkennen.",
        "cta": "Maak uw account aan", "expiry": "Deze uitnodiging is <b>14 dagen</b> geldig — tot {expires}.",
        "fallback": "Als de knop niet werkt, plak deze link in uw browser:",
        "more": "Meer informatie op", "disc": "OpenElpis levert onderzoekshypothesen voor gekwalificeerde professionals — nooit diagnose of behandeladvies. Als u deze e-mail niet verwachtte, kunt u die negeren.",
    },
}


def enabled() -> bool:
    return bool(SMTP_LOGIN and SMTP_KEY)


def _send(to: str, subject: str, html: str, text: str) -> bool:
    if not enabled():
        log.warning("email not configured (no BREVO_SMTP_*); skipping send to %s", to)
        return False
    msg = EmailMessage()
    msg["From"]     = formataddr((MAIL_FROM_NAME, MAIL_FROM))
    msg["To"]       = to
    msg["Subject"]  = subject
    msg["Reply-To"] = MAIL_FROM
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.ehlo(); s.starttls(context=ctx); s.ehlo()
        s.login(SMTP_LOGIN, SMTP_KEY)
        s.send_message(msg)
    return True


def _clean(name: str) -> str:
    """Sanitize an inviter name for safe use in a Subject header / body."""
    return (name or "A colleague").replace("\r", " ").replace("\n", " ").strip()[:120]


def _render(L, inviter, approved, link, expires):
    head  = L["head_app"] if approved else L["head_inv"]
    intro = (L["intro_app"] if approved else L["intro_inv"]).format(inviter=_esc(inviter))
    expiry = L["expiry"].format(expires=expires)
    html = f"""\
<!doctype html><html><body style="margin:0;background:#f4faf8;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#13211e">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:30px 16px">
<table role="presentation" width="540" cellpadding="0" cellspacing="0" style="max-width:540px;background:#ffffff;border:1px solid #dde7e4;border-radius:18px;overflow:hidden;box-shadow:0 12px 32px -16px rgba(7,63,62,.18)">
<tr><td style="background:linear-gradient(180deg,#0a5b5a,#073f3e);padding:22px 34px">
<div style="font-family:Georgia,'Times New Roman',serif;font-size:21px;font-weight:600;color:#ffffff">🎗️ OpenElpis</div></td></tr>
<tr><td style="padding:28px 34px 4px">
<div style="font-family:Georgia,serif;font-size:22px;font-weight:600;color:#0a5b5a;margin-bottom:12px">{_esc(head)} 🎗️</div>
<p style="margin:0 0 14px;font-size:15.5px;line-height:1.6">{intro}</p>
<p style="margin:0 0 24px;font-size:14.5px;line-height:1.6;color:#42524d">{_esc(L["what"])}</p>
<table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="border-radius:999px;background:#a82f5f">
<a href="{link}" style="display:inline-block;padding:13px 30px;font-size:15px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:999px">{_esc(L["cta"])} &rarr;</a>
</td></tr></table>
<p style="margin:18px 0 0;font-size:13.5px;color:#42524d">{expiry}</p>
<p style="margin:14px 0 0;font-size:12.5px;color:#6c7d78">{_esc(L["fallback"])}<br><span style="word-break:break-all;color:#0a5b5a">{link}</span></p>
</td></tr>
<tr><td style="padding:22px 34px 26px">
<hr style="border:0;border-top:1px solid #eef1f0;margin:0 0 14px">
<p style="margin:0 0 10px;font-size:12.5px;color:#6c7d78">{_esc(L["more"])} <a href="{SITE}" style="color:#0a5b5a;font-weight:600">openelpis.com</a></p>
<p style="margin:0;font-size:11.5px;color:#9aa8a4;line-height:1.5">{_esc(L["disc"])}</p>
</td></tr></table></td></tr></table></body></html>"""
    # plain-text alternative (strip the simple <b> tags)
    plain_intro = intro.replace("<b>", "").replace("</b>", "")
    plain_exp   = expiry.replace("<b>", "").replace("</b>", "")
    text = (f"{head}\n\n{plain_intro}\n\n{L['what']}\n\n{L['cta']}: {link}\n\n{plain_exp}\n\n"
            f"{L['more']} {SITE}\n\n{L['disc']}")
    return html, text


def try_send_invite(to, link, expires_at, inviter=None, approved=False, lang="en") -> bool:
    """Best-effort invite/approval email, localized to `lang`. Returns True if sent.
    Never raises. `expires_at` is a datetime; rendered as DD.MM.YYYY (locale-neutral)."""
    if not to:
        return False
    L = LOCALES.get((lang or "en").lower(), LOCALES["en"])
    inviter = _clean(inviter)
    expires = expires_at.strftime("%d.%m.%Y") if expires_at else ""
    subject = (L["subj_app"] if approved else L["subj_inv"].format(inviter=inviter))
    html, text = _render(L, inviter, approved, link, expires)
    try:
        return _send(to, subject, html, text)
    except Exception as e:  # noqa: BLE001 — email must never break invite creation
        log.warning("invite email to %s failed: %s", to, e)
        return False
