# SysAdminMaster

**Supervision et administration courante d'une ferme Windows Remote Desktop Services (RDS) par agents — sans WinRM.**

[Read in English](README.md)

SysAdminMaster donne à une petite équipe informatique un seul endroit pour voir et agir sur une ferme RDS :
sessions en cours, CPU / RAM / mémoire validée, disques, services, profils utilisateurs, et santé des jetons de
licence Microsoft 365 (Office) sur chaque hôte de session. Un agent PowerShell léger tourne en `SYSTEM` sur
chaque hôte, **envoie** sa télémétrie en HTTP et **récupère** les commandes qui l'attendent. Aucun port entrant,
pas de WinRM, aucun identifiant d'administrateur du domaine stocké sur le serveur.

> **État :** en production sur une ferme de 10 hôtes depuis mi-2026. L'interface est en **français**.

## Fonctionnalités

- **Flotte** : état de chaque hôte (en ligne, occupé, muet), version de l'agent, CPU, RAM, mémoire validée,
  disques, services, processus ; mise à jour des agents à distance, par vagues.
- **Sessions** : sessions actives et déconnectées, consommation par utilisateur, message ciblé, déconnexion,
  fermeture de session, prise de contrôle et capture.
- **Profils** : taille de chaque profil, analyse par dossier, nettoyage des téléchargements anciens **simulé**
  d'abord, avec le détail de ce qui serait supprimé.
- **Licences Microsoft 365** : âge du jeton Office de chaque utilisateur sur chaque hôte, état SCA, périmètre par
  OU de l'annuaire, deux gestes de réparation nommés et un diagnostic en lecture seule.
- **Alertes** : contrôle quotidien des jetons périmés envoyé par courriel (Mailjet), avec l'historique de chaque
  contrôle et de sa livraison.
- **Outils de maintenance** : nettoyage des fichiers temporaires, RAM, spouleur, DNS, diagnostic RDS, audit de
  sécurité — chacun une action explicite et autorisée.
- **API en lecture seule** : un jeton distinct pour les tableaux de bord et outils de supervision, refusé sur
  toute commande.

## Démarrage rapide

Il faut un serveur Linux avec **Python 3.11+**, des hôtes Windows avec **PowerShell 5.1**, et un accès HTTP des
hôtes vers le serveur sur le port TCP 5000.

```bash
git clone https://github.com/lugio/sysadminmaster.git
cd sysadminmaster
python3 -m venv .venv-linux
./.venv-linux/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # renseigner les jetons et le mot de passe d'administration
./start-linux.sh                         # AgentMaster sur :5000, tableau de bord sur :5050
```

Puis, sur chaque hôte de session (PowerShell en administrateur) :

```powershell
.\Scripts\Install-SysAdminMaster-Agent.ps1 -BrokerUrl "http://sam-master:5000/api/agent/metrics" -Token "<SAM_AGENT_TOKEN>"
```

## Documentation

La documentation détaillée est en anglais dans [docs/](docs/README.md) : installation, configuration, agents,
API, modèle de sécurité, tests et décisions d'architecture.

## Contribuer

Toute contribution est bienvenue, en français comme en anglais : signalement de bug, documentation, traduction,
code. Lisez [CONTRIBUTING.md](CONTRIBUTING.md) et le [code de conduite](CODE_OF_CONDUCT.md). Une faille de
sécurité se signale **en privé** : voir [SECURITY.md](SECURITY.md).

## Licence

[MIT](LICENSE) © Les contributeurs de SysAdminMaster.
