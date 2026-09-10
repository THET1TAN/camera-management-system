# Investigation : pause au relâchement PTZ

Cette PR part de la v0.2.6 r8 intégrée dans main. Elle prépare la recherche sur la
pause; elle ne prétend pas encore la corriger. La version installée à la racine
locale et le snapshot `~v0.2.6` restent la r8 validée.

## Constats

- La r8 maintient le zoom avec le déplacement et arrête correctement les directions
  relâchées lors de l'essai utilisateur. Le délai natif annoncé est utilisé.
- Les réponses Stop de l'essai r6 prennent environ 220 à 270 ms avant la reprise.
- Pan et Tilt partagent un groupe Stop. La mise à zéro directe essayée en r7 a
  réintroduit une direction bloquée sur la caméra réelle; elle ne peut pas devenir
  le comportement par défaut sur la base des seuls tests simulés.
- TinyCam en ONVIF Profile S cumule déplacement latéral et zoom optique. Ses
  requêtes exactes ne sont pas connues et son comportement au relâchement d'une
  diagonale n'a pas été comparé. GetStatus ne fournit pas de retour exploitable.

## Mesure ajoutée, désactivée par défaut

`ptz_http_timing.py` observe les requêtes existantes via un transport délégué et
le hook de réponse de Requests. Il ne crée aucune commande, ne modifie pas les
arguments ou les délais, et ne parallélise pas Stop et reprise.

Sur la branche de cette PR, activer la mesure pour un lancement de test :

```powershell
$env:CAMERA_PTZ_TRACE_HTTP = '1'
python camera_viewer.py
```

Conserver le mode d'arrêt/reprise par défaut. Dans un nouveau PTZ, répéter quelques
fois une diagonale puis le relâchement d'une direction, avec et sans zoom. Terminer
par le relâchement complet et la fermeture du Viewer. Utiliser une installation
de test configurée avec sa clé et sa base existantes; la préparation locale isolée
de la PR ne contient pas ces fichiers privés.

Le journal local contient une entrée `http_timing_enabled`, puis des entrées
`http_timing` pour Stop et ContinuousMove :

| Champ | Mesure |
| --- | --- |
| `headers_seconds` | Temps entre l'appel du transport et le hook de réponse HTTP |
| `after_headers_seconds` | Temps après ce hook jusqu'au retour du transport |
| `transport_seconds` | Durée totale du transport |
| `status_code` / `error_type` | Code HTTP ou classe d'erreur, sans message privé |

Ces durées incluent le fonctionnement de Requests (connexion, authentification,
réception, hooks); elles ne mesurent pas directement les moteurs. Une grande part
après les en-têtes peut orienter vers la réception du corps ou le traitement réseau.
Une attente avant les en-têtes peut inclure le serveur, la connexion ou l'authentification.
Une valeur inconnue est `null`, jamais une durée nulle inventée.

Les identifiants, adresses, en-têtes et corps SOAP ne sont pas enregistrés. Les
réponses, erreurs et ordre des commandes doivent rester identiques. Un échec de
la mesure ne peut pas empêcher un Stop. Aucun diagnostic visuel n'est ajouté.

Pour désactiver la mesure après fermeture des fenêtres de test :

```powershell
Remove-Item Env:CAMERA_PTZ_TRACE_HTTP
```

## Critères avant une modification du mouvement

1. Identifier la part du délai qui peut réellement être réduite avec la mesure.
2. Garder les arrêts explicites, le délai natif et la demande clavier la plus récente.
3. Ne pas envoyer la reprise avant un Stop non confirmé : une réponse tardive peut
   arrêter le nouveau mouvement ou rejouer une direction devenue périmée.
4. Tester toutes les diagonales, les relâchements séparés, Shift/Ctrl, les changements
   rapides, la perte de focus et la fermeture en cascade.
5. Comparer avant/après sur caméra. Une amélioration mesurée ne doit pas réintroduire
   le blocage observé en r7. La PR reste en brouillon jusqu'à cette validation.

Vérification locale : `python -m unittest discover -s tests`.

La préparation passe 127 tests, dont 13 sur cette instrumentation. Un essai avec
les vraies bibliothèques ONVIF/Requests et un serveur limité à 127.0.0.1 distingue
les délais avant/après les en-têtes pour quatre commandes ordonnées, jusqu'au Stop
final. La pause de la caméra n'a pas encore été mesurée avec cette instrumentation.

Références : [hooks Requests](https://requests.readthedocs.io/en/latest/user/advanced/#event-hooks),
[transport Zeep](https://docs.python-zeep.org/en/master/transport.html),
[ONVIF PTZ, ContinuousMove et Stop](https://www.onvif.org/specs/srv/ptz/ONVIF-PTZ-Service-Spec-v250a.pdf).
