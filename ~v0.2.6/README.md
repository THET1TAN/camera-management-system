# Camera Management System — r8 candidate retirée

**Brouillon, non validé : ne pas utiliser comme version recommandée.**

La r8 est conservée sans correctif de code pour une éventuelle reprise. Les essais réels ont révélé un arrêt de l’axe encore maintenu après un relâchement partiel et un zoom saccadé pendant les déplacements. Voir [l’issue #7](https://github.com/THET1TAN/camera-management-system/issues/7) et [le bilan](../docs/ptz-r8-withdrawal.md).

`main` contient la base publique précédente. La r9 évolue séparément dans la [PR #4](https://github.com/THET1TAN/camera-management-system/pull/4). Aucune réintégration de la r8 sans nouveaux essais et validation explicite.

Installation de test : Python, les dépendances de `requirements.txt`, puis `python camera_viewer.py`. Conserver la base privée et sa clé ensemble. Ne jamais les publier. Le dossier `~v0.2.6` conserve les sources r8 avec le même statut non validé.
