#!/usr/bin/env python3
"""
Veille d'actualité -> Telegram, avec hiérarchisation et résumé quotidien.
Un bot Telegram par thème (politique, économie, sport, art).

Utilisation :
  python veille.py                 # un passage : alertes + résumé si c'est l'heure
  python veille.py --boucle 3      # tourne en continu, vérifie toutes les 3 minutes
  python veille.py --resume        # envoie le résumé du jour tout de suite
  python veille.py --tester        # vérifie que chaque flux RSS répond (n'envoie rien)
  python veille.py --theme sport   # ne traite qu'un thème

Variables d'environnement :
  TOKEN_POLITIQUE, TOKEN_ECONOMIE, TOKEN_SPORT, TOKEN_ART   tokens donnés par @BotFather
  CHAT_ID                                                     ton identifiant Telegram
  ANTHROPIC_API_KEY  (optionnel)                              synthèse rédigée par IA dans le résumé
"""
import argparse
import html
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

DOSSIER = Path(__file__).resolve().parent
FICHIER_SOURCES = DOSSIER / "sources.yaml"
DOSSIER_ETAT = DOSSIER / "etat"
PARIS = ZoneInfo("Europe/Paris")
MAX_ENVOIS_PAR_PASSAGE = 25      # anti-spam, par thème
GARDER_IDS = 6000                # articles mémorisés pour ne jamais renvoyer un doublon
FENETRE_SUJETS = timedelta(hours=24)
GARDER_ARTICLES = timedelta(hours=36)
SEUIL_SIMILARITE = 0.32          # à quel point deux articles doivent se ressembler pour former un sujet
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
MODELE_IA = "claude-haiku-5-5"
NIVEAUX = {1: "⚪", 2: "🟠", 3: "🔴"}
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

MOTS_VIDES = set("""
le la les un une des du de d l au aux et ou mais donc or ni car que qui quoi dont ou ce cet cette ces
son sa ses leur leurs mon ma mes ton ta tes notre nos votre vos il elle ils elles on nous vous je tu
est sont etre ete avoir avait a ont fait faire plus moins tres pas ne se sur sous dans par pour avec
sans entre apres avant depuis pendant contre vers chez comme aussi encore deja tout tous toute toutes
selon face alors ainsi cela celui celle ceux quand comment pourquoi peut doit veut va vont sera
deux trois premier premiere nouveau nouvelle nouveaux selon hier aujourd hui demain annee annees jour
ans an fois etait direct video photos info infos article lire suite
the a an of to in on for with by at from and or but is are was were be been has have had will would
it its this that these those as not after before over new says said how why what who into about
""".split())


# ───────────────────────────── utilitaires ─────────────────────────────

def charger_env_local():
    f = DOSSIER / ".env"
    if f.exists():
        for ligne in f.read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if ligne and not ligne.startswith("#") and "=" in ligne:
                k, v = ligne.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def maintenant():
    return datetime.now(timezone.utc)


def iso(d):
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def depuis_iso(s):
    return datetime.fromisoformat(s)


def nettoyer(texte, longueur=280):
    texte = re.sub(r"<[^>]+>", " ", texte or "")
    texte = html.unescape(re.sub(r"\s+", " ", texte)).strip()
    if len(texte) <= longueur:
        return texte
    return texte[: longueur - 1].rsplit(" ", 1)[0] + "…"


def e(t):
    return html.escape(t or "", quote=True)


def sans_accents(t):
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def mots(texte):
    texte = sans_accents(texte.lower())
    res = []
    for m in re.findall(r"[a-z0-9]+", texte):
        if len(m) < 3 or m in MOTS_VIDES or m.isdigit() and len(m) < 4:
            continue
        if len(m) > 4 and m.endswith("s"):
            m = m[:-1]
        res.append(m)
    return res


def date_fr(d):
    d = d.astimezone(PARIS)
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}"


# ───────────────────────────── configuration ─────────────────────────────

def charger_config():
    conf = yaml.safe_load(FICHIER_SOURCES.read_text(encoding="utf-8"))
    reg = conf.get("reglages", {})
    themes = conf["themes"]
    for nom, t in themes.items():
        flux = []
        for f in t.get("flux", []):
            if isinstance(f, str):
                f = {"url": f}
            f.setdefault("nom", re.sub(r"^https?://(www\.)?([^/]+).*", r"\2", f.get("url") or f.get("google", "")))
            f.setdefault("poids", 1)
            f.setdefault("rubrique", "")
            flux.append(f)
        t["flux"] = flux
        t.setdefault("alertes", reg.get("alertes", "sujets"))
        t.setdefault("emoji", "📰")
    return reg, themes


# ───────────────────────────── état (mémoire) ─────────────────────────────

def charger_etat(theme):
    f = DOSSIER_ETAT / f"{theme}.json"
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == 2:
            return data, False
        # ancienne version (simple liste) : on garde les IDs déjà vus
        return {"version": 2, "vus": list(data) if isinstance(data, list) else [],
                "articles": [], "annonces": {}, "dernier_resume": None, "pannes": {}}, False
    return {"version": 2, "vus": [], "articles": [], "annonces": {},
            "dernier_resume": None, "pannes": {}}, True


def sauver_etat(theme, etat):
    DOSSIER_ETAT.mkdir(exist_ok=True)
    etat["vus"] = etat["vus"][-GARDER_IDS:]
    ids = {a["id"] for a in etat["articles"]}
    etat["annonces"] = {k: v for k, v in etat["annonces"].items() if k in ids}
    etat["decisions"] = {k: v for k, v in etat.get("decisions", {}).items() if k in ids}
    (DOSSIER_ETAT / f"{theme}.json").write_text(
        json.dumps(etat, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ───────────────────────────── collecte ─────────────────────────────

ENTETES = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


def lire_flux(url):
    try:
        r = requests.get(url, headers=ENTETES, timeout=20)
        r.raise_for_status()
        d = feedparser.parse(r.content)
        if not d.entries and d.bozo:
            return [], f"pas un flux RSS valide ({d.bozo_exception.__class__.__name__})"
        return d.entries, None
    except Exception as ex:
        return [], str(ex)[:150]


def requete_google(url):
    """Déduit 'site:media.fr/rubrique' à partir de l'adresse d'un flux."""
    m = re.match(r"https?://([^/?]+)(/[^?]*)?", url)
    if not m:
        return None
    hote = re.sub(r"^(www|services|dwh|feeds|rss|syndication)\.", "", m.group(1))
    segments = [s for s in (m.group(2) or "").split("/") if s]
    rubrique = ""
    if len(segments) > 1 and not re.search(r"rss|feed|xml|flux|api|arc", segments[0], re.I):
        rubrique = "/" + segments[0]
    return f"site:{hote}{rubrique}"


def lire_google_actualites(requete):
    """Relais de secours : les articles d'un média vus par Google Actualités (dernières 24 h)."""
    url = ("https://news.google.com/rss/search?q=" + requests.utils.quote(f"{requete} when:1d")
           + "&hl=fr&gl=FR&ceid=FR:fr")
    entrees, err = lire_flux(url)
    for en in entrees:  # Google ajoute " - Nom du média" à la fin des titres
        src = (en.get("source") or {}).get("title")
        if src and en.get("title", "").endswith(" - " + src):
            en["title"] = en["title"][: -len(src) - 3]
        en["summary"] = ""  # le résumé Google ne fait que répéter le titre
    return entrees, err


def lire_source(f):
    """Lit le flux direct ; en cas d'échec (ou si 'google:' est indiqué), passe par Google Actualités."""
    if f.get("google"):
        e2, err2 = lire_google_actualites(f["google"])
        return e2, err2, "google"
    entrees, err = lire_flux(f["url"])
    if not err:
        return entrees, None, "direct"
    req = requete_google(f["url"])
    if req:
        e2, err2 = lire_google_actualites(req)
        if not err2 and e2:
            return e2, None, f"relais Google ({req})"
    return [], err, "panne"


def date_publication(entree):
    t = entree.get("published_parsed") or entree.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def collecter(theme, conf, etat, tester=False):
    """Lit tous les flux du thème, ajoute les nouveaux articles à l'état, renvoie leurs IDs."""
    vus = set(etat["vus"])
    # même titre chez le même média = même article (évite les doublons flux direct / relais Google)
    titres_vus = {(a["source"], sans_accents(a["titre"].lower())) for a in etat["articles"]}
    nouveaux = []
    now = maintenant()
    for f in conf["flux"]:
        cle = f.get("url") or f.get("google")
        entrees, err, mode = lire_source(f)
        if tester:
            etat_txt = f"ERREUR {err}" if err else f"OK {len(entrees)} articles ({mode})"
            print(f"  [{theme}] {etat_txt:<60} {f['nom']}")
            continue
        if err:
            p = etat["pannes"].setdefault(cle, {"nom": f["nom"], "depuis": iso(now), "nb": 0})
            p["nb"] += 1
            p["erreur"] = err
            print(f"  [{theme}] flux en panne : {f['nom']} ({err})")
            continue
        if mode.startswith("relais"):
            print(f"  [{theme}] {f['nom']} : flux direct indisponible, {mode} OK")
        etat["pannes"].pop(cle, None)
        for en in entrees:
            uid = en.get("id") or en.get("link") or en.get("title")
            if not uid or uid in vus:
                continue
            vus.add(uid)
            etat["vus"].append(uid)
            # Recherche Google Actualités : on crédite le vrai média qui a publié l'article
            source = f["nom"]
            if f.get("google"):
                source = (en.get("source") or {}).get("title") or source
            cle_titre = (source, sans_accents(nettoyer(en.get("title", ""), 250).lower()))
            if cle_titre in titres_vus:
                continue
            titres_vus.add(cle_titre)
            pub = date_publication(en)
            if pub and now - pub > GARDER_ARTICLES:
                continue  # vieux contenu remonté dans un flux : ignoré
            etat["articles"].append({
                "id": uid,
                "titre": nettoyer(en.get("title", "(sans titre)"), 250),
                "resume": nettoyer(en.get("summary", ""), 400),
                "lien": en.get("link", ""),
                "source": source,
                "poids": f["poids"],
                "rubrique": f["rubrique"],
                "date": iso(pub or now),
                "vu_le": iso(now),
            })
            nouveaux.append(uid)
    limite = now - GARDER_ARTICLES
    etat["articles"] = [a for a in etat["articles"] if depuis_iso(a["vu_le"]) >= limite]
    return set(nouveaux)


# ───────────────────────────── hiérarchisation ─────────────────────────────

def filtre_ok(a, conf):
    contenu = f"{a['titre']} {a['resume']}".lower()
    cles = [m.lower() for m in conf.get("mots_cles") or []]
    exclus = [m.lower() for m in conf.get("mots_exclus") or []]
    if cles and not any(m in contenu for m in cles):
        return False
    return not any(m in contenu for m in exclus)


def regrouper(articles):
    """Regroupe les articles qui parlent du même sujet (similarité TF-IDF des titres + chapôs)."""
    if not articles:
        return []
    docs = []
    for a in articles:
        tokens = mots(a["titre"]) * 2 + mots(a["resume"][:300])  # le titre compte double
        tf = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        docs.append(tf)
    n = len(docs)
    df = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
    vecs = []
    for d in docs:
        v = {t: c * idf[t] for t, c in d.items()}
        norme = math.sqrt(sum(x * x for x in v.values())) or 1
        vecs.append({t: x / norme for t, x in v.items()})

    ordre = sorted(range(n), key=lambda i: articles[i]["vu_le"])
    groupes = []  # {"centre": vec, "membres": [i]}
    for i in ordre:
        v = vecs[i]
        meilleur, score_max = None, 0.0
        for g in groupes:
            c = g["centre"]
            s = sum(x * c.get(t, 0) for t, x in v.items())
            if s > score_max:
                meilleur, score_max = g, s
        if meilleur and score_max >= SEUIL_SIMILARITE:
            meilleur["membres"].append(i)
            somme = dict(meilleur["somme"])
            for t, x in v.items():
                somme[t] = somme.get(t, 0) + x
            norme = math.sqrt(sum(x * x for x in somme.values())) or 1
            meilleur["somme"] = somme
            meilleur["centre"] = {t: x / norme for t, x in somme.items()}
        else:
            groupes.append({"centre": v, "somme": dict(v), "membres": [i]})
    return [[articles[i] for i in g["membres"]] for g in groupes]


def evaluer(sujet, reg):
    """Score = somme des poids des médias distincts (+1 si mot d'urgence). Renvoie (niveau, score)."""
    poids = {}
    for a in sujet:
        poids[a["source"]] = max(poids.get(a["source"], 0), a.get("poids", 1))
    score = sum(poids.values())
    textes = " ".join(sans_accents(a["titre"].lower()) for a in sujet)
    if any(re.search(r"\b" + re.escape(sans_accents(str(m).lower())) + r"\b", textes)
           for m in reg.get("mots_importants", [])):
        score += 1
    if score >= reg.get("seuil_majeur", 5):
        return 3, score
    if score >= reg.get("seuil_important", 3):
        return 2, score
    return 1, score


def representant(sujet):
    """L'article qui illustre le mieux le sujet : média au plus gros poids, puis le plus ancien."""
    return sorted(sujet, key=lambda a: (-a.get("poids", 1), a["vu_le"]))[0]


def liens_sources(sujet, maxi=6):
    vus, liens = set(), []
    for a in sorted(sujet, key=lambda a: (-a.get("poids", 1), a["vu_le"])):
        if a["source"] in vus or not a["lien"]:
            continue
        vus.add(a["source"])
        liens.append(f'<a href="{e(a["lien"])}">{e(a["source"])}</a>')
    reste = len(vus) - maxi
    txt = " · ".join(liens[:maxi])
    return txt + (f" (+{reste})" if reste > 0 else "")


def nb_medias(sujet):
    return len({a["source"] for a in sujet})


# ───────────────────────────── Telegram ─────────────────────────────

def telegram(token, chat_id, texte, apercu=True):
    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": texte, "parse_mode": "HTML",
                      "disable_web_page_preview": not apercu},
                timeout=20)
        except requests.RequestException as ex:
            print(f"    ! Telegram injoignable : {ex}")
            time.sleep(3)
            continue
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            print(f"    ! Telegram a refusé : {r.text[:200]}")
        time.sleep(1.1)  # ~1 message/seconde maximum
        return r.ok
    return False


def telegram_long(token, chat_id, blocs):
    """Envoie une suite de blocs en respectant la limite de 4096 caractères par message."""
    message = ""
    for b in blocs:
        if len(message) + len(b) > 3900 and message:
            telegram(token, chat_id, message, apercu=False)
            message = ""
        message += b
    if message:
        telegram(token, chat_id, message, apercu=False)


def message_article(a, conf, niveau, raison=""):
    badge = f"{NIVEAUX[niveau]} " if niveau >= 2 else ""
    rub = f" · {e(a['rubrique'])}" if a.get("rubrique") else ""
    t = f"{conf['emoji']} {badge}<b>{e(a['titre'])}</b>\n"
    if a["resume"] and a["resume"].lower()[:60] not in a["titre"].lower():
        t += f"\n{e(nettoyer(a['resume'], 280))}\n"
    if raison:
        t += f"\n💡 <i>{e(raison)}</i>\n"
    heure = depuis_iso(a["date"]).astimezone(PARIS).strftime("%H:%M")
    t += f"\n<i>{e(a['source'])}{rub} · {heure}</i>"
    if a["lien"]:
        t += f'\n<a href="{e(a["lien"])}">Lire l\'article</a>'
    return t


def message_escalade(sujet, conf, niveau):
    rep = representant(sujet)
    libelle = "SUJET MAJEUR" if niveau == 3 else "Sujet important"
    t = (f"{conf['emoji']} {NIVEAUX[niveau]} <b>{libelle}</b> — {nb_medias(sujet)} médias en parlent\n\n"
         f"<b>{e(rep['titre'])}</b>\n")
    if rep["resume"]:
        t += f"\n{e(nettoyer(rep['resume'], 280))}\n"
    t += f"\n📎 {liens_sources(sujet, 8)}"
    return t


# ───────────────────────────── tri éditorial par IA ─────────────────────────────

def appel_ia(consigne, max_tokens):
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
        "content-type": "application/json"},
        json={"model": MODELE_IA, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": consigne}]})
    r.raise_for_status()
    texte = "".join(b.get("text", "") for b in r.json()["content"])
    return json.loads(texte[texte.find("{"): texte.rfind("}") + 1])


def demander_tri(theme, conf, reg, lot):
    """lot = [(sujet, niveau_media)]. Renvoie {index: {"g": bool, "p": 1-3, "r": str}} ou None si échec."""
    lignes = []
    for i, (s, niv) in enumerate(lot, 1):
        rep = representant(s)
        rub = f"[{rep['rubrique']}] " if rep.get("rubrique") else ""
        autres = [a["titre"] for a in s if a is not rep][:2]
        medias = ", ".join(sorted({a["source"] for a in s})[:5])
        ligne = f"{i}. {rub}{rep['titre']} — {rep['resume'][:220]} ({nb_medias(s)} média(s) : {medias})"
        if autres:
            ligne += " | Autres titres : " + " / ".join(autres)
        lignes.append(ligne)
    consigne = (
        f"Tu es le rédacteur en chef de ce média : {reg.get('presentation', '').strip()}\n\n"
        f"Rubrique « {theme} ». Ligne éditoriale :\n{conf.get('ligne_editoriale', '').strip()}\n\n"
        "Voici des sujets d'actualité qui viennent de sortir :\n\n" + "\n".join(lignes) + "\n\n"
        "Pour chaque sujet, décide s'il faut prévenir le journaliste. Sois sélectif : garde seulement "
        "ce qui correspond clairement à la ligne éditoriale. Ne laisse jamais passer à côté d'un "
        "événement vraiment majeur, même s'il est à la limite de la ligne.\n"
        "Priorité : 1 = info utile, 2 = important, 3 = incontournable (tout le monde va en parler).\n"
        "Réponds uniquement en JSON, sans autre texte : "
        "{\"1\": {\"g\": true, \"p\": 2, \"r\": \"pourquoi en 10 mots max\"}, \"2\": {\"g\": false}, ...}")
    try:
        data = appel_ia(consigne, 120 + 45 * len(lot))
        res = {}
        for k, v in data.items():
            if isinstance(v, dict) and str(k).isdigit():
                res[int(k)] = {"g": bool(v.get("g")), "p": min(3, max(1, int(v.get("p", 1) or 1))),
                               "r": str(v.get("r", ""))[:120]}
        return res
    except Exception as ex:
        print(f"  [{theme}] tri IA indisponible pour l'instant : {ex}")
        return None


def tri_actif(reg, conf):
    return bool(conf.get("tri_ia", reg.get("tri_ia", True))) and bool(os.environ.get("ANTHROPIC_API_KEY"))


def juger(theme, conf, reg, etat, sujets, inclure_init=False):
    """
    Renvoie une liste de (sujet, garder, niveau_final, score, raison).
    garder = None si la décision n'a pas pu être prise (IA indisponible) : on réessaiera.
    """
    resultats = []
    a_juger = []  # (position dans resultats, sujet, niveau)
    decisions = etat.setdefault("decisions", {})
    actif = tri_actif(reg, conf)

    for s in sujets:
        niveau, score = evaluer(s, reg)
        if not actif:
            resultats.append([s, True, niveau, score, ""])
            continue
        ds = [decisions.get(a["id"]) for a in s]
        connues = [d for d in ds if d and d.get("g") is not None]
        gardees = [d for d in connues if d["g"]]
        manquantes = any(d is None for d in ds)
        if gardees:
            meilleure = max(gardees, key=lambda d: d.get("p", 1))
            resultats.append([s, True, max(niveau, meilleure.get("p", 1)), score, meilleure.get("r", "")])
            for a in s:
                decisions.setdefault(a["id"], meilleure)
        elif connues:
            # rejeté : on redemande seulement si le sujet a pris de l'ampleur depuis
            if niveau > max(d.get("n", 1) for d in connues):
                resultats.append([s, None, niveau, score, ""])
                a_juger.append((len(resultats) - 1, s, niveau))
            else:
                resultats.append([s, False, niveau, score, ""])
                for a in s:
                    decisions.setdefault(a["id"], connues[0])
        elif manquantes or inclure_init:
            resultats.append([s, None, niveau, score, ""])
            a_juger.append((len(resultats) - 1, s, niveau))
        else:
            resultats.append([s, False, niveau, score, ""])  # sujet du premier lancement, sans activité

    for debut in range(0, len(a_juger), 40):
        lot = a_juger[debut:debut + 40]
        reponse = demander_tri(theme, conf, reg, [(s, niv) for _, s, niv in lot])
        if reponse is None:
            continue  # pas de décision enregistrée : on réessaiera au prochain passage
        for i, (pos, s, niv) in enumerate(lot, 1):
            v = reponse.get(i, {"g": False, "p": 1, "r": ""})
            d = {"g": v["g"], "p": v["p"], "r": v["r"], "n": niv}
            for a in s:
                decisions[a["id"]] = d
            resultats[pos][1] = v["g"]
            if v["g"]:
                resultats[pos][2] = max(niv, v["p"])
                resultats[pos][4] = v["r"]
            else:
                resultats[pos][1] = False
    if a_juger:
        print(f"  [{theme}] tri IA : {len(a_juger)} sujet(s) évalué(s), "
              f"{sum(1 for r in resultats if r[1] is True and any(r[0] is s for _, s, _ in a_juger))} retenu(s)")
    return [tuple(r) for r in resultats]


# ───────────────────────────── alertes en direct ─────────────────────────────

def alertes(theme, conf, reg, etat, nouveaux, token, chat_id, premier):
    now = maintenant()
    recents = [a for a in etat["articles"]
               if now - depuis_iso(a["vu_le"]) <= FENETRE_SUJETS and filtre_ok(a, conf)]
    sujets = regrouper(recents)
    mode = conf["alertes"]
    envois = []  # (texte, apercu)

    if premier:
        for s in sujets:
            niveau, _ = evaluer(s, reg)
            for a in s:
                etat["annonces"][a["id"]] = niveau
                etat.setdefault("decisions", {})[a["id"]] = {"g": None}
        print(f"[{theme}] initialisé : {len(recents)} articles mémorisés, rien envoyé.")
        if token:
            tri = "activé 🧠" if tri_actif(reg, conf) else "désactivé (pas de clé ANTHROPIC_API_KEY)"
            telegram(token, chat_id,
                     f"{conf['emoji']} <b>Bot {theme} activé</b>\nSources suivies : {len({f['nom'] for f in conf['flux']})}.\n"
                     f"Tri éditorial par IA : {tri}.\nRésumé quotidien à {reg.get('heure_resume', 20)}h.")
        return

    if mode == "aucune":
        # mode "résumé seul" : on collecte en silence, le tri et l'envoi se font à l'heure du résumé
        print(f"[{theme}] {len(nouveaux)} nouveaux articles collectés (pas d'alerte, résumé à "
              f"{reg.get('heure_resume', 20)}h).")
        return

    ecartes = 0
    for sujet, garder, niveau, score, raison in juger(theme, conf, reg, etat, sujets):
        if not garder:
            ecartes += garder is False and any(a["id"] in nouveaux for a in sujet)
            continue
        ids = [a["id"] for a in sujet]
        deja = max((etat["annonces"].get(i, 0) for i in ids), default=0)
        neufs = [a for a in sujet if a["id"] in nouveaux]
        annonce = deja
        if mode == "toutes":
            for a in neufs:
                envois.append((message_article(a, conf, niveau, raison), True))
            if neufs:
                annonce = max(deja, 1)
            if niveau == 3 and deja < 3:
                envois.append((message_escalade(sujet, conf, 3), False))
                annonce = 3
        elif mode == "sujets":
            if deja == 0:
                envois.append((message_article(representant(neufs or sujet), conf, niveau, raison), True))
                annonce = max(1, niveau)
            elif niveau == 3 and deja < 3:
                envois.append((message_escalade(sujet, conf, 3), False))
                annonce = 3
        elif mode == "importantes":
            if niveau >= 2 and niveau > deja:
                envois.append((message_escalade(sujet, conf, niveau), False))
                annonce = niveau
        for i in ids:
            etat["annonces"][i] = annonce

    if len(envois) > MAX_ENVOIS_PAR_PASSAGE:
        print(f"[{theme}] {len(envois)} alertes, limitées à {MAX_ENVOIS_PAR_PASSAGE}")
        envois = envois[-MAX_ENVOIS_PAR_PASSAGE:]
    for texte, apercu in envois:
        telegram(token, chat_id, texte, apercu)
    print(f"[{theme}] {len(nouveaux)} nouveaux articles, {len(envois)} alertes envoyées"
          + (f", {ecartes} sujet(s) écarté(s) par le tri." if ecartes else "."))


# ───────────────────────────── résumé quotidien ─────────────────────────────

def selection_ia(theme, conf, reg, candidats, nombre):
    """
    L'IA choisit les `nombre` sujets les plus importants parmi les candidats et les rédige
    (titre court + 2 phrases). Renvoie [(index_candidat, titre, resume)] ou None si indisponible.
    """
    if not os.environ.get("ANTHROPIC_API_KEY") or not candidats:
        return None
    lignes = []
    for i, s in enumerate(candidats, 1):
        extraits = "\n".join(f"   - [{a['source']}] {a['titre']} — {a['resume'][:220]}" for a in s[:5])
        lignes.append(f"Sujet {i} ({nb_medias(s)} média(s)) :\n{extraits}")
    consigne = (
        f"Tu es le rédacteur en chef de ce média : {reg.get('presentation', '').strip()}\n\n"
        f"Rubrique « {theme} ». Ligne éditoriale :\n{conf.get('ligne_editoriale', '').strip()}\n\n"
        "Voici les sujets du jour (le nombre de médias qui en parlent indique leur ampleur) :\n\n"
        + "\n\n".join(lignes) + "\n\n"
        f"Choisis AU MAXIMUM {nombre} sujets : les plus importants du jour pour un post Instagram/TikTok, "
        "du plus important au moins important. Moins s'il n'y a pas assez de sujets vraiment intéressants.\n"
        "Pour chacun écris :\n"
        "- \"titre\" : un titre clair et percutant, 10 mots maximum, compréhensible sans contexte ;\n"
        "- \"resume\" : 2 phrases courtes maximum, simples et factuelles (qui, quoi, chiffre clé si utile).\n"
        "Appuie-toi uniquement sur les extraits, n'invente aucun fait, pas d'opinion.\n"
        "Réponds uniquement en JSON : {\"top\": [{\"n\": 3, \"titre\": \"...\", \"resume\": \"...\"}, ...]}")
    try:
        data = appel_ia(consigne, 1200)
        res = []
        for item in data.get("top", [])[:nombre]:
            n = int(item.get("n", 0))
            if 1 <= n <= len(candidats) and n - 1 not in [r[0] for r in res]:
                res.append((n - 1, str(item.get("titre", "")).strip(), str(item.get("resume", "")).strip()))
        return res or None
    except Exception as ex:
        print(f"  [{theme}] sélection IA indisponible : {ex}")
        return None


def liens_courts(sujet, maxi=3):
    """Les liens vers les articles des médias principaux (pour creuser le sujet)."""
    return liens_sources(sujet, maxi)


def resume_quotidien(theme, conf, reg, etat, token, chat_id):
    now = maintenant()
    debut = depuis_iso(etat["dernier_resume"]) if etat.get("dernier_resume") else now - FENETRE_SUJETS
    debut = max(debut, now - FENETRE_SUJETS)
    articles = [a for a in etat["articles"] if depuis_iso(a["vu_le"]) >= debut and filtre_ok(a, conf)]
    nombre = int(reg.get("sujets_dans_resume", 5))

    sujets = regrouper(articles)
    notes = []
    for s, garder, niveau, score, raison in juger(theme, conf, reg, etat, sujets, inclure_init=True):
        if garder:
            notes.append((niveau, score, len(s), s))
    notes.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)

    candidats = [s for *_, s in notes[:15]]
    choix = selection_ia(theme, conf, reg, candidats, nombre)
    if choix is None:  # sans IA : les sujets les plus couverts, avec le titre et le chapô d'origine
        choix = [(i, representant(s)["titre"], nettoyer(representant(s)["resume"], 200))
                 for i, s in enumerate(candidats[:nombre])]

    texte = f"{conf['emoji']} <b>{theme.upper()} · {date_fr(now)}</b>\n\n"
    if not choix:
        texte += "Rien de marquant aujourd'hui."
    for rang, (i, titre, resume) in enumerate(choix, 1):
        texte += f"<b>{rang}. {e(titre)}</b>\n"
        if resume:
            texte += f"{e(resume)}\n"
        texte += f"🔗 {liens_courts(candidats[i])}\n\n"

    if token:
        telegram_long(token, chat_id, [texte.strip()])
    else:
        print(texte)
    etat["dernier_resume"] = iso(now)
    pannes = [p["nom"] for p in etat["pannes"].values() if now - depuis_iso(p["depuis"]) > timedelta(hours=6)]
    if pannes:
        print(f"[{theme}] flux en panne depuis plus de 6 h : {', '.join(pannes)}")
    print(f"[{theme}] résumé envoyé : {len(choix)} sujet(s) sur {len(notes)} retenus par le tri.")


def heure_du_resume(reg, etat):
    now_paris = maintenant().astimezone(PARIS)
    if now_paris.hour < reg.get("heure_resume", 20):
        return False
    dernier = etat.get("dernier_resume")
    return not dernier or depuis_iso(dernier).astimezone(PARIS).date() < now_paris.date()


# ───────────────────────────── programme principal ─────────────────────────────

def traiter(theme, conf, reg, chat_id, forcer_resume=False):
    token = os.environ.get(conf["token_env"], "")
    if not token:
        print(f"[{theme}] secret {conf['token_env']} absent -> thème ignoré")
        return
    etat, premier = charger_etat(theme)
    if not etat["articles"] and etat["vus"]:
        premier = True  # migration depuis l'ancienne version : on remet à niveau sans envoyer
    if "decisions" not in etat:
        # mise à jour depuis la version sans tri : les articles déjà connus ne sont pas réévalués
        etat["decisions"] = {a["id"]: {"g": None} for a in etat["articles"]}
    nouveaux = collecter(theme, conf, etat)
    alertes(theme, conf, reg, etat, nouveaux, token, chat_id, premier)
    if forcer_resume or (not premier and heure_du_resume(reg, etat)):
        resume_quotidien(theme, conf, reg, etat, token, chat_id)
    sauver_etat(theme, etat)


def main():
    charger_env_local()
    p = argparse.ArgumentParser()
    p.add_argument("--boucle", type=float, metavar="MINUTES")
    p.add_argument("--tester", action="store_true")
    p.add_argument("--resume", action="store_true", help="envoie le résumé du jour maintenant")
    p.add_argument("--theme")
    args = p.parse_args()

    reg, themes = charger_config()
    if args.theme:
        themes = {args.theme: themes[args.theme]}
    if args.tester:
        for theme, conf in themes.items():
            collecter(theme, conf, {"vus": [], "articles": [], "pannes": {}}, tester=True)
        return
    chat_id = os.environ.get("CHAT_ID", "")
    if not chat_id:
        sys.exit("CHAT_ID manquant (voir le README).")

    while True:
        for theme, conf in themes.items():
            try:
                traiter(theme, conf, reg, chat_id, forcer_resume=args.resume)
            except Exception as ex:  # un thème en erreur ne bloque pas les autres
                print(f"[{theme}] ERREUR : {ex}")
        if not args.boucle or args.resume:
            break
        time.sleep(args.boucle * 60)


if __name__ == "__main__":
    main()
