# r9 C — essai de reprise anticipée

**Expérimental, en attente d'essai utilisateur.** La r9 B reste le meilleur
candidat testé : relâchements corrects et déplacement avec zoom, avec une pause.
`main` et la r8 retirée ne sont pas modifiés. La référence B est le commit
`96ea754ab59f1de55b171c4f62927797ea6efb42` ; son moteur séquentiel reste inchangé.

## Observation préalable

Le moteur B n'ajoute pas d'attente volontaire entre le neutre et la reprise.
Sur la session utilisateur B, la réponse au neutre prenait 272 ms en médiane.
Une comparaison de commandes uniquement nulles sur la caméra, sans mouvement
non nul, donne les médianes suivantes (3 requêtes par variante, caméra à l'arrêt) :

| Commande | Médiane |
| --- | --- |
| ContinuousMove nul, délai natif explicite | 252 ms |
| ContinuousMove nul, délai laissé au défaut | 245 ms |
| RelativeMove avec translation nulle | 242 ms |

Ces petits échantillons ne permettent pas de conclure à une différence utile.
Remplacer simplement la commande neutre ne paraît pas résoudre la pause.

[ONVIF PTZ §5.3](https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec.pdf)
prévoit que les mouvements peuvent être remplacés et demande de minimiser la
latence, sans garantir un délai précis. Il ne garantit pas l'ordre d'exécution
de requêtes concurrentes. L'essai C ne suppose donc pas qu'une réponse reçue
prouve l'ordre réel des mouvements.

## Ce que teste C

Lors d'une transition nécessitant le neutre, un client ONVIF séparé envoie
uniquement un vecteur nul. Après environ 60 ms, si sa réponse est toujours en
attente et qu'un mouvement reste demandé, le moteur essaie une seule reprise
anticipée avec le dernier état des axes. Le délai réel dépend de l'ordonnancement
et figure dans le journal. Ce délai n'est pas une mesure de la pause mécanique.

Les clients possèdent des objets Zeep, des authentifications WSSE et des sessions
HTTP distincts. Le thread auxiliaire ne peut envoyer que du neutre. Il ne modifie
pas l'état du moteur, ne relance pas de mouvement et n'exécute pas de preset.

Une seule transition peut être en cours, avec au plus une reprise anticipée.
Après la fin des deux requêtes, le moteur relit les touches et réapplique l'état
complet. Cette réapplication traite le cas où le neutre s'exécute après la reprise.
Elle peut toutefois produire une seconde pause ou une saccade sur certains
appareils ; il faut observer ce point dans le test physique.

Relâchement total, expiration du signal clavier, fermeture, perte de focus ou
preset empêchent une nouvelle reprise anticipée. Le Stop définitif intervient
après la fin des requêtes déjà engagées, afin qu'aucune ancienne reprise ne
parte après lui. Une erreur de l'une des requêtes impose un Stop avant reprise.
Les délais réseau des deux clients PTZ sont réglés à une seconde. En cas de
coupure réseau, la durée native caméra demeure la limite du dernier mouvement
accepté, comme pour B ; aucune garantie d'arrêt physique sans communication.

Ce mode peut réduire la pause si la caméra applique le neutre avant de répondre.
Il peut aussi n'apporter aucun gain si elle sérialise ses opérations, ou provoquer
des saccades si elle les exécute dans un ordre défavorable. Aucun fabricant n'est
ciblé et aucune activation automatique n'est faite. B reste disponible tel quel.

## Lancer le test quand disponible

Fermer les anciens contrôleurs, puis lancer dans cette branche :

```powershell
python camera_viewer_early_resume_test.py
```

Le PTZ doit afficher **r9 - Early resume test C**. Le lanceur active uniquement
pour son processus et ses enfants `CAMERA_PTZ_EARLY_RESUME=1`, les transitions
neutres et la mesure HTTP. Il n'ajoute pas de diagnostic à l'interface.

1. Maintenir S + A, puis relâcher S en gardant A. Comparer la pause à B :
   plus courte, identique, plus longue ou double pause. L'axe relâché doit s'arrêter.
2. Maintenir une diagonale + Shift pendant 5 secondes puis relâcher un axe.
   Refaire avec Ctrl. Le zoom et le dernier axe doivent rester continus.
3. Changer rapidement de direction puis tout relâcher. Vérifier l'arrêt complet,
   puis la fermeture du Viewer avec ses fenêtres enfants.

Si un axe reste bloqué, relâcher toutes les touches, appuyer sur Échap et revenir
au lanceur B. Pour B : `python camera_viewer_direct_test.py`. Celui-ci force
explicitement `CAMERA_PTZ_EARLY_RESUME=0`, même si le terminal l'avait activé.
Le test utilisateur se fait librement, sans attente programmée ni délai de réponse.

## Vérifications avant essai physique

- **159 tests réussis**, dont reprise avant réponse, neutre exécuté en retard,
  changements rapides, relâchement total, expiration, fermeture, perte de focus,
  preset, erreurs des deux requêtes et impossibilité de démarrer le thread.
- Vraies requêtes ONVIF/Zeep/Requests sur un serveur local : deux clients et
  sessions distincts, reprise avant réponse, exécution tardive du neutre,
  réapplication du dernier état et Stop final.
- Démarrage et fermeture du PTZ C en Tk masqué ; connexion réelle atteignant
  la création du PTZ avec les deux clients, sans mouvement dans cette vérification.

Les événements locaux `early_neutral_send`, `early_resume_send` et
`early_reconcile` permettent de comparer l'attente au résultat observé.
Les journaux sont bornés et ne contiennent pas d'identifiants ni de corps SOAP.
La réduction de pause et la fiabilité physique de C restent à valider.
