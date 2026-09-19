# Reprendre le parcours réel, caméra par caméra

## Ce qui est établi et ce qui reste à mesurer

Avant modification, les 132 fichiers suivis hors snapshots présents à la racine
ont été comparés au commit `67cd6026f0fd6d6e8780686b889c4c4dd722ffc8` : aucune
différence de contenu, hors fins de ligne. Le checkout Git de la PR était propre.

| Blocage | Cause établie | Ce qui n’est pas encore établi |
|---|---|---|
| Import `cryptography` | La trace utilisateur vient de Python 3.14, tandis que les deux processus Viewer observés utilisent Python 3.9. L’import échoue avant l’exécution de tout test. | La suite complète et les dépendances natives de lecture ne sont pas qualifiées par ce constat. |
| Fuseau CGI | Aucun `playback.json` n’était présent ; `camera.zone` était donc vide. Le fuseau d’affichage ne le remplace pas. Le lien entre ID de base et caméra CGI a été vérifié localement et la confirmation Toronto sauvegardée uniquement pour cet ID. | La corrélation image/heure et les cas DST doivent encore être vérifiés. |
| Réponses ISAPI/auto | Le code abandonnait tous les résultats lorsqu’une piste suivante levait une erreur, et ne conservait pas toutes les causes de détection. Ces défauts ont été corrigés. | Aucun nouveau relevé caméra ne prouve que la piste 103 explique les erreurs de ce banc. Les étapes exactes restent à relever. |
| Namespaces | Le parser retirait déjà les namespaces. Les fixtures reprennent le namespace réellement observé et la structure des XML sauvegardés. | Un problème de namespace n’est pas démontré par le message générique initial. |

Les commandes ci-dessous sont à lancer volontairement dans PowerShell, depuis
la racine `Camera`. Fermer les anciennes fenêtres au moment choisi pour charger
le code corrigé. L’agent n’a arrêté ni lancé aucune application sur le poste.

## 1. Environnement, sans caméra ni lecteur

```powershell
$CameraPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python39\python.exe'
& $CameraPython tools/diagnose_playback.py environment
& $CameraPython -m pip install -r requirements.txt
& $CameraPython -m unittest discover -s tests -p "test_playback*.py" -v
```

Le diagnostic d’environnement ne dépend que de la bibliothèque standard. Il
affiche `sys.executable`, version, architecture, `prefix`/`base_prefix`, éventuel
environnement virtuel, résolution de `python` dans PATH, choix du lanceur et
présence/version des modules. Il signale un éventuel ajout de dépendances
temporaires dans le chemin Python. Il n’importe pas les dépendances optionnelles,
ne crée pas de clé et ne lance ni FFmpeg, ni VLC, ni réseau. La présence d’un module
n’est pas une validation de son fonctionnement natif.

Ne pas utiliser simplement `python` ou `pip`. Les lanceurs respectent
`CAMERA_PYTHON` s’il est défini ; sinon ils choisissent le Python 3.9 indiqué
ci-dessus. Playback intégré partage le processus du Viewer. Ses travailleurs
caméra et libVLC sont créés avec `sys.executable` ; leurs messages indiquent la
version observée et si l’interpréteur correspond au parent. Ces informations
sont visibles dans Détails lorsque les processus ont effectivement démarré.

## 2. Recherche ciblée, sans vidéo

Choisir l’ID réel de la base, vérifié dans le Viewer, et une journée récente
contenant des enregistrements. L’ID n’est jamais dérivé de l’adresse réseau.

```powershell
$CameraId = [int](Read-Host 'ID de la caméra ISAPI à diagnostiquer')
$Jour = (Get-Date).AddDays(-1).ToString('yyyy-MM-dd')
& $CameraPython tools/diagnose_playback.py camera --camera $CameraId --date $Jour --backend isapi --track 101
```

Cette commande détecte le modèle via les réponses observées, découvre les pistes,
puis cherche **uniquement 101**. Elle ne télécharge aucun média. 101 est ici la
piste validée lors des anciens essais de ce banc, pas une règle par défaut du code.

Pour la comparaison demandée, utiliser ensuite :

```powershell
& $CameraPython tools/diagnose_playback.py camera --camera $CameraId --date $Jour --compare-tracks --track 101
```

Séquence : A = ISAPI/101 ; B = chaque autre piste découverte séparément ; C =
auto sans restriction de piste. Chaque processus est fermé avant le suivant.
Les pistes secondaires sont limitées à huit par défaut ; toute limite est
signalée. `Enable` est affiché, sans servir de garantie ni masquer les archives
d’une piste actuellement désactivée. Les requêtes d’une journée sont paginées
avec `searchResultPostion`, le même `searchID` par recherche et les timestamps UTC.

Une piste réussie reste exploitable si une autre échoue. La réponse indique
`complete=false`, `tracks-partial` et la cause précise ; aucune couverture complète
n’est inventée. Si toutes les pistes échouent, leurs causes restent disponibles.
Une réponse vide valide est distinguée d’une réponse invalide.

Pour la caméra CGI, sélectionner son propre ID et confirmer son fuseau dans
Réglages avant la recherche :

```powershell
$CameraId = [int](Read-Host 'ID de la caméra CGI à diagnostiquer')
& $CameraPython tools/diagnose_playback.py camera --camera $CameraId --date $Jour --backend videolink
```

Le backend CGI inclut le jour précédent pour retrouver une archive chevauchant
minuit ; il n’explore pas le mois. Les paramètres restent `year/month/day`,
`stream=-1`, `record_mode=-1`, `media_type=3` et le UID privé. La requête de
téléchargement conserve le filepath retourné, via `/playback/`, lorsque le lecteur
la demandera. Un UID expiré peut être renouvelé ; une réponse arbitrairement
invalide ne provoque plus une reconnexion masquant le premier rejet.

## 3. Lecture intégrée sur cette même caméra/journée

Quand sa recherche retourne des archives, ouvrir explicitement :

```powershell
& $CameraPython camera_playback.py --camera $CameraId --date $Jour --day-only
```

La fenêtre conserve calendrier, filtres, timeline et responsive. Dans ce mode,
seule la caméra indiquée est initialement sélectionnée et seule la journée
choisie est interrogée ; les autres dates restent en cache/non interrogées.
Choisir une heure réellement listée dans `sample` (convertie dans le fuseau
d’affichage), puis suivre sélection → téléchargement → préparation → lecture.
La vidéo ne démarre pas à l’ouverture du calendrier. Pour isoler 101 dans ce
parcours, choisir localement `isapi` et `101` dans Réglages, puis Appliquer.

Le cas « fuseau caméra requis » propose dans Détails un bouton lié à **l’ID
concerné**. Appliquer sauvegarde localement, arrête les anciens propriétaires,
recharge les caméras par ID puis relance cette caméra/journée. Aucune réouverture
n’est nécessaire pour ce changement et aucune horloge caméra n’est modifiée.
Une date CGI ambiguë reste refusée ; aucun UTC−4 permanent n’est utilisé.

Fermer complètement le lecteur avant l’essai de la caméra suivante. La suite
globale peut ouvrir des fenêtres : la réserver à un créneau approprié.

## Diagnostic à transmettre si l’étape échoue encore

Les sorties `protocol`, `error` et `search-result` du diagnostic caméra, ou les
événements `camera-protocol` de `%LOCALAPPDATA%\CameraManagementSystem\playback\events.jsonl`,
contiennent : ID local, backend, étape, méthode et endpoint fixe sans paramètres,
statut HTTP, type de contenu, octets reçus, racine/namespace XML, code applicatif,
piste demandée/retournée, position de pagination, compte et motif du rejet.
Les corps HTTP non lus sont comptés à zéro, sans inventer une taille reçue.

La console caméra et les journaux excluent XML brut, adresses de caméras,
identifiants, mots de passe, UID, cookies, Authorization et playbackURI. Le chemin
d’un téléchargement CGI est remplacé par `/playback/<recording>`. Les codes
applicatifs inconnus sont indiqués comme non reconnus plutôt que copier une
chaîne arbitraire. La sonde ONVIF d’identité réutilise le helper existant et
rapporte seulement sa structure XML/son échec ; ses headers/octets HTTP ne sont
pas instrumentés. Détails garde 32 événements par caméra et le journal tourne.

Conserver chaque sortie de comparaison avec l’action effectuée. Ne transmettre
ni XML brut ni fichier vidéo ni base/clé privée. Un test synthétique réussi ne
valide pas le parcours réel. La PR reste en brouillon et l’issue ouverte.
