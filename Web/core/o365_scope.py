"""Périmètre « utilisateurs Office 365 » — décidé côté SERVEUR.

Pourquoi ici et pas dans l'agent
--------------------------------
L'agent remonte le `distinguishedName` AD brut de chaque profil et ne décide
RIEN. Coder l'OU dans le script agent obligerait à une **mise à jour de flotte**
à chaque changement de périmètre — l'opération la plus risquée du projet (elle a
détruit la flotte le 23/06/2026). Ici, changer de périmètre est une variable
d'environnement et un redémarrage de service.

Pourquoi l'OU et pas un groupe de licence
-----------------------------------------
Sur bien des annuaires, il n'existe aucun groupe de licence O365 exploitable : l'OU
des comptes synchronisés vers Entra est alors le meilleur critère disponible.
`SAM_O365_SCOPE_OU` la désigne ; vide, aucun filtre d'OU n'est appliqué (tous les
profils connus de l'AD sont dans le périmètre).

Ce que le filtre écarte
-----------------------
Sur une ferme réelle, le filtre écartait typiquement des comptes locaux (absents
de l'AD), des comptes d'administration présents sur plusieurs hôtes, et des
départs rangés dans une OU d'archive (`SAM_O365_SCOPE_EXCLUDE`).

⚠️ Écarter n'est pas cacher. Des profils de clients ou de stagiaires peuvent être
vus sur la ferme : hors tenant, donc hors alerte — mais la page doit pouvoir les
montrer, d'où la bascule « Tous ». Un périmètre qui rend invisible ce qu'il exclut
reproduit le défaut qu'il corrige.
"""
import os
import re

# OU retenue (sous-arbre inclus) et sous-OU explicitement exclues.
try:
    from Web.core import settings as _settings
except ImportError:                                   # lancé depuis Web/core
    import settings as _settings
SCOPE_OU = _settings.o365_scope_ou()                  # vide = pas de filtre d'OU
SCOPE_EXCLUDE = _settings.o365_scope_exclude()


# Comptes qui ne sont PAS des utilisateurs Office, reconnaissables au NOM seul.
# Ils n'ont pas besoin d'un DN pour être écartés : c'est structurel. L'agent
# applique déjà ces règles au scan, mais la vue cumulative relit l'historique —
# elle ressuscitait donc des profils que l'agent d'aujourd'hui ne remonterait
# plus (constaté : `TEMP` et le compte machine du broker réapparaissaient dans
# le périmètre « Office 365 »). Une lecture rétrospective doit rejouer les filtres
# du présent, sinon elle exhume ce que le code a appris à ignorer.
# Comptes propres à un site (prestataire, service, logiciel métier) :
# SAM_O365_IGNORED_ACCOUNTS, liste séparée par des virgules, s'ajoute à celle-ci.
SYSTEM_ACCOUNTS = {
    'administrateur', 'administrator', 'admin', 'temp', 'guest', 'invité',
    'uvhdcleanupbin', 'defaultuser0', 'public', 'default',
    'default user', 'all users', 'systemprofile', 'localservice', 'networkservice',
} | _settings.o365_ignored_accounts()


def is_technical(name):
    """Compte technique reconnaissable au nom, sans aucun besoin d'AD."""
    if not name:
        return False
    n = str(name)
    b = n.lower()
    if b in SYSTEM_ACCOUNTS:
        return True
    if n.endswith('$'):                       # compte machine
        return True
    if re.match(r'^TEMP(\.|$)', n, re.I):     # profil temporaire
        return True
    if re.match(r'^(administrateur|administrator|admin)[._]', b):   # comptes d'admin nominatifs (admin_xx)
        return True
    if re.search(r'\.\d{3}$', n):             # profil duplique (.000, .001…)
        return True
    return False


def classify(dn, name=None):
    """Catégorie d'un profil à partir de son DN AD.

    Retourne l'un de : 'o365' (dans le périmètre), 'exclu' (dans l'OU mais dans
    une sous-OU exclue), 'hors_perimetre' (connu de l'AD, ailleurs), 'hors_ad'
    (aucun DN — compte local ou de service).

    ⚠️ `hors_ad` est une INFORMATION, pas une absence de mesure : c'est ce qui
    distingue un compte local d'un collaborateur. Un agent antérieur à la 2.29.0
    ne remonte pas de DN du tout — il produit donc `hors_ad` pour tout le monde,
    et c'est pourquoi l'UI doit dire quand elle ne sait pas (voir `dn_known`).
    """
    if is_technical(name):
        return 'technique'
    if not dn:
        return 'hors_ad'
    up = dn.upper()
    if any(x.upper() in up for x in SCOPE_EXCLUDE):
        return 'exclu'
    if SCOPE_OU.upper() in up:
        return 'o365'
    return 'hors_perimetre'


def ou_path(dn):
    """Chemin d'OU lisible, pour dire à l'utilisateur POURQUOI un profil est écarté."""
    if not dn:
        return None
    parts = [p.strip() for p in dn.split(',') if p.strip().upper().startswith('OU=')]
    return ','.join(parts) if parts else '(racine)'


def annotate(hosts):
    """Ajoute `scope` et `ou` à chaque utilisateur, et `dn_known` à chaque hôte.

    N'ÉCARTE RIEN : annoter et filtrer sont deux gestes distincts. Un champ de
    périmètre qui filtre en silence est un défaut classique (un champ qui annote sans filtrer, ou
    l'inverse, est tout aussi trompeur) — ici l'appelant choisit.
    """
    for h in hosts:
        users = h.get('users')
        if not isinstance(users, list):
            continue
        known = 0
        for u in users:
            if not isinstance(u, dict):
                continue
            # Le DN est deja resolu en amont par license_view (historique +
            # cache serveur) : annoter ne doit pas redecider de la source.
            dn = u.get('dn')
            u['scope'] = classify(dn, u.get('name'))
            u['ou'] = ou_path(dn)
            known += 1 if dn else 0
        # 🔴 Trois etats, pas deux : True (au moins un DN), False (des profils,
        # aucun DN -> agent trop ancien), None (aucun profil -> RIEN A DIRE).
        # La 1re version rendait True quand l'hote n'avait aucun profil : un hote
        # vide « prouvait » alors que le perimetre etait disponible, et l'UI
        # masquait tout le monde en silence au lieu de prevenir. Un hote sans
        # donnee ne temoigne de rien — c'est le meme piege que « un vide se lit
        # rien a signaler ».
        h['dn_known'] = (known > 0) if users else None
    return hosts


def counts(hosts):
    """Compteurs par catégorie, sur les hôtes qui ont Office."""
    out = {'o365': 0, 'exclu': 0, 'hors_perimetre': 0, 'hors_ad': 0, 'technique': 0}
    for h in hosts:
        if h.get('office_installed') is False:
            continue
        for u in (h.get('users') or []):
            cat = (u or {}).get('scope')
            if cat in out:
                out[cat] += 1
    return out
