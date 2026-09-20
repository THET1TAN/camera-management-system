# PR #16 : sélection, export, scrubbing et stockage

Révision de départ vérifiée : `3b732cd2aebb347a7a650f92b7fb1016300f7010`.
La racine de Camera contenait les mêmes 88 fichiers que la branche de la PR,
sans modification locale divergente. Le journal du dernier essai porte
`0.2.11-dev-boundaries-1` et l'empreinte SHA-256
`12635622db0a56a680b1d5eb38af3c6f04af86d5d90ade6ad8ed5ef31e4f1600`,
identique à celle des modules de cette révision. Python exécuté : **3.9.13**.
Le nouveau marqueur est `0.2.11-dev-consolidated-1` ; chaque démarrage calcule
également l'empreinte exacte des modules présents.

**Développement non qualifié.** Les tests ci-dessous sont préparés, pas exécutés
par l'agent. Aucun lecteur, application, FFmpeg, son, vidéo ou appel caméra n'a
été lancé. Les 44 tests réussis antérieurement par l'utilisateur ne qualifient
pas ces changements. PR en brouillon, issue ouverte, aucune fusion.

## Observations et investigation de la jonction

La correspondance des caméras a été vérifiée dans la base locale en lecture
seule. Les adresses, identifiants et médias ne sont pas publiés.

| Source | Dernier constat utilisateur | Analyse des données conservées |
|---|---|---|
| C1, CGI/VideoLink | Bond entre deux fichiers, repère vertical sur la timeline ; sens et durée inconnus | MP4, HEVC 2592×1944, audio µ-law ; paire candidate `c021420404b1` → `ce1a01115910` |
| C2, ISAPI/Hikvision-like | Transitions visuellement fluides | MPEG, HEVC 3072×1728, audio AAC ; conserver comme témoin |

La paire C1 est **candidate**, pas une identification certaine de l'image
observée : l'utilisateur n'a pas noté l'heure. Son premier fichier commence
à UTC `1789839600`, le suivant à `1789840200`.

| Mesure en lecture seule | A | B |
|---|---:|---:|
| Durée MP4 déclarée | 600 s | 599 s |
| Échantillons vidéo dans STTS | 15009 | 14976 |
| Somme STTS vidéo / 90000 | 600,330 s | 599,010 s |
| Somme STTS audio / 1000 | 600,400 s | 599,048 s |
| Premier PTS vidéo HLS | 1,528 s | 1,528 s |
| Premier PTS audio HLS | 1,400 s | 1,400 s |
| PTS vidéo, dernier segment | 601,498 → 601,818 s | 597,776 → 600,498 s |
| PTS audio, dernier segment | 601,572 s | 597,900 → 600,360 s |
| Dernier EXTINF | 0,330 s | 2,730 s |

Les incréments vidéo usuels sont de 3600 ticks à 90 kHz, soit 25 i/s pour
ces MP4 ; les premières durées diffèrent. Cela ne qualifie pas la cadence C2.
Les PTS audio présentés sont ceux des en-têtes PES, pas tous les échantillons
audio. La préparation fait apparaître un chevauchement de **0,300 s** dans
`archive-boundary`, alors que les durées déclarées se joignent exactement.
Le dépassement existe donc déjà dans les tables du premier original.

La génération native 67 s'ouvre une fois à +511,055 s, environ 89 secondes
avant la frontière. B est publié avant son passage. Les échantillons natifs
encadrent la frontière à +596,191 puis +601,016 s, sans seek ni réouverture
entre ces deux relevés. Leur espacement d'environ cinq secondes **ne prouve
pas** l'absence d'une répétition/perte visuelle. Aucune observation F8 de cette
frontière n'est disponible. L'audio doit aussi être écouté.

Il n'est pas justifié de décaler toutes les caméras ou de raccourcir les EXTINF
pour masquer ce chevauchement. La traduction PTS/DTS/PCR existante et le chemin
vidéo copié restent conservés. **Le bond CGI reste à qualifier par A/B/C** ;
cette livraison ne l'annonce pas corrigé. La tolérance de 0,5 s est uniquement
une condition d'enchaînement, pas une preuve d'exactitude.

## Changements à essayer

- Les messages ont leur propre rangée fixe sous le HWND vidéo. Requested time
  et Preview time restent hors image ; les erreurs longues vont dans Details.
  Le panneau compact occupe une rangée séparée. Le HWND n'est pas recréé.
- Scrubbing : une commande en cours, une seule dernière cible en attente.
  Une cible plus récente peut abandonner le travail précédent après 850 ms ;
  ce seuil est un point de départ à mesurer. Le relâchement prend immédiatement
  priorité, avec une nouvelle confirmation et les contrôles précédents.
- La timeline conserve son fond et ses plages tant que leurs données et la
  fenêtre temporelle ne changent pas. Curseur et sélection ont leurs propres
  éléments. `timeline-render` mesure le coût ; ce coût n'est pas présenté comme
  une cause démontrée du défaut.
- `pointer-target`, `seek-request`, `native-seek-issued`, `native-seek-evidence`,
  `preview-confirmed`, `preview-abandoned` séparent les étapes observables.
  Le résumé rapporte p50/p95 des confirmations. Les compteurs natifs restent
  un indicateur : **ils ne mesurent pas indépendamment les pixels vus**. F8
  enregistre une observation humaine avec son délai de réaction.
- Aucun proxy supplémentaire n'est créé à ce stade. Si les mesures en cache
  chaud restent insuffisantes, une voie d'aperçu dédiée devra être qualifiée.
  Ni 8 commandes/s ni le nouveau régulateur ne garantissent 8 images utiles/s.

## Sélection et export

Activer **Select range**, dessiner une plage, déplacer A/B ou déplacer son
intérieur. La lecture ne change pas. Les champs Start/End acceptent des dates
ISO avec millisecondes et décalage UTC ; les heures locales ambiguës sont
refusées sans décalage explicite. A/B restent utilisables depuis le curseur.
La sélection conserve sa caméra et sa piste pendant un changement de caméra,
le zoom, le défilement, la lecture, minuit et le redimensionnement. Le libellé
indique cette association ; la saisie de piste est obligatoire si plusieurs
pistes sont possibles. Une nouvelle sélection sur une autre caméra remplace
explicitement l'ancienne.

**Export selection — Precise** crée un travail autonome à partir de caméra,
piste, intervalle UTC et destination. Aucune vidéo ouverte n'est nécessaire.
Le travail recherche les jours concernés, inspecte/réutilise les originaux,
télécharge les fichiers nécessaires et traite les portions une par une,
indépendamment de la limite des quatre archives du lecteur.

Les intervalles sont semi-ouverts `[A,B)`. En chevauchement, l'archive au début
le plus récent est prioritaire ; à début égal, révision observée la plus
récente, puis hash. Chaque instant a un seul propriétaire. Les trous ne sont
jamais comblés par la tolérance du lecteur. Une couverture incomplète ou une
erreur réseau ne permet pas de déclarer un trou : l'export s'arrête avec
`export-coverage-unknown` si les sources connues ne couvrent pas la sélection.

Les lacunes confirmées demandent un choix avant encodage : conserver le temps
avec **No recording**, exporter les seules portions disponibles avec leur
liste de lacunes, ou annuler/modifier. Aucun gel d'image n'est substitué au trou.

Les portions utiles sont décodées et encodées H264/PCM en temporaires, puis
l'assemblage est réencodé H264/AAC en MP4 pour normaliser les changements de
paramètres. C'est un traitement de toute la plage utile, **avec deux passages
d'encodage vidéo**, pas une copie sans perte ni une promesse de n'encoder que
les extrémités. Proportions conservées par mise à l'échelle/padding, OSD inclus
dans la source, aucune capture du viewer ni interpolation de mouvement.
Une source sans audio reçoit une piste silencieuse.

Deux threads d'encodage, priorité Windows réduite, une tâche d'export et une
source protégée par étape. Les démarrages de transfert/encodage attendent la
préparation utile du lecteur. Les espaces intermédiaire et final sont estimés
avant encodage ; les budgets et l'espace libre sont contrôlés pendant le travail.
Un export trop volumineux est refusé ; la lecture déjà protégée reste disponible.

Le JSON annexe contient plages demandées/effectives, bornes de paquets vidéo
et audio mesurées, tolérance liée à la cadence, sources expurgées, fuseau,
conversion, lacunes et SHA-256. Les bornes effectives sont calculées depuis les
paquets dérivés et l'origine UTC indexée : la corrélation OSD et la précision
réelle restent à qualifier. Aucun écrasement d'un fichier existant. Destination
obligatoirement hors cache ; en cas d'échec de publication de l'annexe après
la vidéo, le JSON temporaire est conservé près du résultat pour récupération.

## Storage & Cache

Défauts conservés : **8 GiB**, marge libre **2 GiB**, inactivité **14 jours**.
Nettoyage préventif à **90 %**, cible **75 %**, tous configurables sans arrêter
le lecteur via Apply storage settings. Une réduction du plafond ne force pas
la suppression des ressources actives ; de nouvelles allocations peuvent être
refusées jusqu'à libération.

Store possède l'entretien au démarrage, toutes les cinq minutes environ et
après les travaux. Aucun service ou automatisme système. Chaque tâche possède
ses protections comptées, y compris les requêtes HTTP jusqu'à fermeture du
fichier. Les playlists restent protégées ; leur historique est borné à quatre
archives par génération, puis libéré après arrêt du lecteur. Les sources
d'export sont libérées par étape. Les verrous de producteurs disparaissent
avec leur dernier utilisateur.

Les réservations sont partagées et atomiques. Les octets écrits sont comptés
dans l'inventaire physique, seule la réservation restante s'y ajoute. Une
réconciliation forcée précède toute allocation concurrente ; les vérifications
d'un producteur examinent son dossier au plus une fois/seconde, pas tout le
cache à chaque image. Les scans/effacements restent hors Tk, sans conserver le
verrou SQLite pendant le parcours des fichiers.

Le panneau sépare médias, index/journaux, total, réservations, protégés,
supprimables et espace libre. Les logs restent limités à environ 1,5 MiB ;
checkpoint WAL périodique avec cible de journal 16 MiB, fraîcheurs de recherche
anciennes retirées par lots de 500. Au-delà de 128 MiB d'index/WAL, les nouvelles
recherches sont refusées explicitement plutôt que d'effacer la disponibilité.

**Clean unused cache** et **Clear camera cache** proposent une estimation,
demandent confirmation, puis revérifient les protections. Seuls les octets
effectivement effacés sont annoncés. Fichiers verrouillés : conservation/report,
sans tuer d'autre processus. Dossiers inconnus : comptés, conservés, signalés.
Liens/jonctions et fichiers inconnus bloquent l'effacement. La racine de production
doit être dédiée, hors OneDrive ; seuls les bancs explicitement hors ligne
acceptent un cache sous leur dossier de fixture.

Les tâches interrompues sont réconciliées. Les workspaces d'export abandonnés
connus deviennent éligibles ; originaux récemment utilisés conservés selon
rétention/quota. L'index conserve les métadonnées distantes lors d'une éviction,
mais retire le badge cached. Une présence distante peut être ancienne : consulter
sa date de recherche. **Download original** permet de sauvegarder une copie
hors cache avant qu'elle ne soit évincée.

Sur ce poste, **neuf clés d'archives d'incident sont protégées** par le champ
local `protected_archives` de `playback.json`. Journaux/index ont une copie privée
dans `.release-work/consolidated-evidence`. Rien de cela n'est publié. Ne retirer
ces protections qu'après conservation/qualification explicite des preuves.

## Commandes : à lancer soi-même au moment choisi

Préparer l'environnement PowerShell puis exécuter les 114 tests Playback :

```powershell
$Python = 'C:\Users\thetitan.TITANDARK\AppData\Local\Programs\Python\Python39\python.exe'
Set-Location -LiteralPath 'C:\Users\thetitan.TITANDARK\OneDrive\Documents\Informatique\Projet\Camera'
& $Python -m unittest discover -s tests -p 'test_playback*.py' -v
```

Les **30 nouveaux tests** concernent planification multi-archives, overlaps,
trous, minuit, source/track, annulation, réseau non concluant, commandes précises,
leases imbriqués/multiples, réservations concurrentes, quotas, vrais usages,
orphelins, verrous/jonctions simulés, workspaces, sélection et régulation.
Toutes les suppressions de tests emploient des répertoires temporaires synthétiques.
Les tests ne constituent pas une qualification média/visuelle.

Lancer le lecteur normal, une caméra à la fois :

```powershell
& $Python '.\camera_playback.py' --camera 1 --date 2026-09-19 --day-only
# Fermer ce lecteur avant le témoin C2.
& $Python '.\camera_playback.py' --camera 2 --date 2026-09-19 --day-only
```

Créer un nouveau banc privé depuis la **paire CGI candidate exacte**, sans
nettoyer le cache réel. La copie et la préparation utilisent de l'espace disque ;
`--warm` lance FFmpeg lorsque vous exécutez cette commande.

```powershell
$Pair = Join-Path $env:LOCALAPPDATA ('CameraManagementSystem\cgi-pair-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
& $Python '.\tools\playback_boundary_bench.py' --directory $Pair --camera 1 --from-cache --pair-start 1789839600
& $Python '.\tools\playback_boundary_bench.py' --directory $Pair --camera 1 --warm
```

A : ouvrir soi-même `archive-A.bin` puis `archive-B.bin` dans VLC, observer OSD
et son autour de leur limite. B : exécuter le serveur ci-dessous ; il affiche
une URL à ouvrir soi-même dans VLC. Commencer 20 secondes avant la jonction,
à 1× sans geste. Ctrl+C arrête ce seul serveur.

```powershell
& $Python '.\tools\playback_boundary_bench.py' --directory $Pair --camera 1 --serve --before 20
```

C : après fermeture de VLC/du serveur précédent, même paire dans le lecteur :

```powershell
& $Python '.\camera_playback.py' --fixture-directory $Pair --camera 1 --date 2026-09-19 --day-only
```

Relever l'OSD juste avant/après, sens/durée du bond, son et position affichée ;
répéter à 2× puis 4× seulement après 1×. Pour le témoin C2, reprendre d'abord
la jonction déclarée fluide ; ne pas la reclasser comme gelée par défaut.

## Matrice manuelle restante

| Parcours | Vérification demandée | Résultat sur cette révision |
|---|---|---|
| C1 A/B/C ; témoin C2 | OSD/son, pertes/répétitions, horloges, générations | À mesurer |
| Scrubbing chaud, droite/gauche | Images utiles pendant le geste ; F8 distinct des compteurs | À mesurer |
| Scrubbing en réception/frontière | Heure demandée ≠ aperçu ancien ; libération cible finale | À mesurer |
| Pause, 0,5×/1×/2×/4×, mute/volume | Paramètres conservés au relâchement ; fermeture pendant le geste | À mesurer |
| Large, compact, HiDPI | Aucun texte sur OSD, statut stable, champs accessibles par défilement | À mesurer |
| Export 1/2/3 puis >4 fichiers | Exemple 14:02:10→14:12:35 = 625 s si couverture continue | À mesurer |
| Export frontières/minuit/formats | Première/dernière image, aspect, son absent/différent et sync | À mesurer |
| Export avec lacunes | Trois choix, durée maintenue ou liste explicite, overlap unique | À mesurer |
| Export annulation/disque | Pas de final incomplet annoncé réussi ; originaux préservés | À mesurer |
| Cache synthétique + lecteur/export | Nettoyage après confirmation, leases HTTP/playlist, quota réduit | À mesurer |
| Consultation longue | Générations bornées, retour arrière conservé, pas de pins abandonnés | À mesurer |

Rapports expurgés, après les essais :

```powershell
$Log = Join-Path $env:LOCALAPPDATA 'CameraManagementSystem\playback\events.jsonl'
& $Python '.\tools\summarize_playback_events.py' --log $Log --camera 1
& $Python '.\tools\summarize_playback_events.py' --log $Log --camera 2
```

La syntaxe Python 3.9 et la correspondance racine/branche sont des vérifications
statiques, pas des essais réussis. Le [guide précédent](archive-playback-boundaries.md)
reste un historique daté ; ses anciens constats sur C2 ne remplacent pas le
dernier retour utilisateur. Le pont RTSP/cache reste hors implémentation.

Références : [FFmpeg, accurate seek et encodage](https://ffmpeg.org/ffmpeg.html),
[concat et exigences des flux](https://ffmpeg.org/ffmpeg-formats.html#concat).
