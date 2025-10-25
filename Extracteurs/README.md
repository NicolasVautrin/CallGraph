# Extracteurs - Architecture du système d'extraction du Call Graph

Système d'extraction du call graph Java/XML pour projets Axelor avec cache intelligent et stockage dans ChromaDB.

## Vue d'ensemble

Le système extrait les relations entre symboles (méthodes, classes, champs) depuis :
- **Projets Axelor locaux** (modules/)
- **Dépendances Axelor** (axelor-open-platform, axelor-open-suite)

Et stocke les résultats dans une **base vectorielle ChromaDB** pour requêtes rapides via le serveur MCP.

## Architecture globale

```
┌─────────────────────────────────────────────────────────────┐
│                   MCP CallGraph Server                      │
│  (mcp_callgraph_server.py)                                  │
│  - API MCP pour interroger le call graph                    │
│  - Auto-détecte .vector-raw-db ou .vector-semantic-db       │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ExtractionManager                         │
│  - Orchestration de l'extraction complète                   │
│  - Gestion du cache Axelor                                  │
│  - Fusion des bases de données                              │
└────────┬──────────────────────┬─────────────────────────────┘
         │                      │
         ▼                      ▼
┌─────────────────┐      ┌─────────────────┐
│ AxelorRepoMgr   │      │  StorageWriter  │
│ - Détection     │      │  - ChromaDB     │
│   versions      │      │  - Indexation   │
│ - Download      │      │  - Requêtes     │
│ - Cache DB      │      └─────────────────┘
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│                       Extracteurs                           │
├────────────────────┬────────────────────┬───────────────────┤
│  JavaASTExtractor  │  AxelorXmlExtractor│  TypeScriptAST..  │
│  - Scan *.java     │  - Scan *.xml      │  - Scan *.ts/js   │
│  - Via service     │  - Parse direct    │  - AST analysis   │
│  - Parallélisé     │  - Parallélisé     │  - À venir        │
└────────┬───────────┴────────────────────┴───────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────┐
│               JavaASTService (Java)                         │
│  - Service HTTP REST (port 8765)                            │
│  - JavaParser + JavaSymbolSolver                            │
│  - Cache de parsers par ensemble de repos                   │
│  - Résolution FQN et emplacements                           │
└─────────────────────────────────────────────────────────────┘
```

## Flux d'extraction

### Mode "full" : Extraction complète avec Axelor

```
1. Détection des versions Axelor
   ExtractionManager → AxelorRepoManager.detect_axelor_versions()
   ├─> Lit gradle.properties et build.gradle
   └─> Trouve platform=8.2.x, suite=8.2.x

2. Download des repos Axelor (si nécessaire)
   AxelorRepoManager.ensure_repos()
   └─> git clone axelor-open-platform vX.X.X
   └─> git clone axelor-open-suite vX.X.X
       Destination: axelor-repos/axelor-open-platform-X.X.X/
                    axelor-repos/axelor-open-suite-X.X.X/

3. Vérification des caches
   AxelorRepoManager.has_cached_db()
   ├─> Cherche axelor-open-platform-X.X.X/.vector-raw-db/
   ├─> Cherche axelor-open-suite-X.X.X/.vector-raw-db/
   └─> Si manquant → extraction nécessaire

4. Extraction des repos manquants
   Pour chaque repo Axelor sans cache :

   ExtractionManager._extract_all_repos()
   ├─> JavaASTExtractor(repos=[platform, suite, project])
   │   └─> Extraction parallèle avec routing par source_file
   │       ├─> Entries platform → cache platform
   │       ├─> Entries suite → cache suite
   │       └─> Entries project → DB projet
   │
   └─> AxelorXmlExtractor(repos=[platform, suite, project])
       └─> Extraction parallèle avec routing par source_file

5. Fusion des caches dans la DB projet
   ExtractionManager.copy_from_cache()
   ├─> Copie platform/.vector-raw-db/ → projet/.vector-raw-db/
   ├─> Fusionne suite/.vector-raw-db/
   └─> Les entries projet sont déjà dans la DB

   → Résultat: DB complète avec tous les symboles résolus
```

### Mode "local" : Extraction modules uniquement

```
1. Extraction locale
   ExtractionManager.extract_local()
   └─> JavaASTExtractor(repos=[modules])
   └─> AxelorXmlExtractor(repos=[modules])

   Note: Résolution FQN limitée (seulement modules/)
```

## Stratégie de cache

### Cache de bases vectorielles

**Localisation** :
```
axelor-repos/
├─ axelor-open-platform-X.X.X/
│  ├─ .vector-raw-db/         # Cache DB pour platform
│  └─ .vector-semantic-db/    # Cache DB avec embeddings (optionnel)
└─ axelor-open-suite-X.X.X/
   ├─ .vector-raw-db/         # Cache DB pour suite
   └─ .vector-semantic-db/    # Cache DB avec embeddings (optionnel)

projet/
└─ .vector-raw-db/            # DB finale (Axelor + local)
   ou .vector-semantic-db/
```

**Avantages** :
- Extraction Axelor : une fois par version
- Projets multiples : réutilisation des caches
- Gain de temps : 70-90% sur re-extraction

### Cache de parsers (JavaASTService)

**En mémoire dans JavaASTService** :
```java
Map<Set<String>, JavaParser> parserCache = {
  {platform, suite} → JavaParser #1,
  {platform, suite, modules} → JavaParser #2
}
```

**Lifetime** : Tant que le service tourne

**Avantages** :
- Évite de recréer les TypeSolver (3-5s par parser)
- Résolution FQN optimale avec tous les repos
- Cache automatique par combinaison de repos

## Composants

### 1. ExtractionManager.py

**Rôle** : Orchestration de l'extraction complète

**Méthodes principales** :
- `ensure_axelor_repos()` : Download repos Axelor si nécessaire
- `extract_full(reset, use_embeddings)` : Extraction complète avec cache
- `extract_local(use_embeddings)` : Extraction locale uniquement
- `_extract_all_repos(repos, all_repos, project_db)` : Extrait tous les repos avec routing intelligent

**Flux extract_full()** :
1. Détecte versions Axelor
2. Download repos manquants
3. Vérifie caches existants
4. Extrait repos manquants → caches (en parallèle)
5. Copie caches → projet DB
6. Extrait local → projet DB (avec résolution complète)

**Nouveautés v2.0** :
- Extraction parallèle avec routing par `source_file`
- Une seule passe d'extraction pour tous les repos
- Fusion optimisée des caches
- Support debug avec statistiques détaillées

### 2. AxelorRepoManager.py

**Rôle** : Gestion des dépendances Axelor

**Méthodes principales** :
- `detect_axelor_versions()` : Lit gradle.properties et build.gradle
- `ensure_repos(platform_version, suite_version)` : Download si manquants
- `has_cached_db(repo_name, version, use_embeddings)` : Vérifie existence cache
- `get_cached_db_path(repo_name, version, use_embeddings)` : Retourne path cache

**Détection de versions** :
```python
# gradle.properties
axelorVersion=8.2.9
axelorSuiteVersion=8.2.9

# build.gradle
com.axelor:axelor-gradle:8.2.9
```

### 3. JavaASTExtractor.py

**Rôle** : Extraction usages Java via JavaASTService

**Classe** : `JavaASTExtractor`

**Méthodes** :
- `__init__(repos)` : Initialise avec liste de repos
- `discover_java_files()` : Trouve tous les .java (exclut build/, node_modules/)
- `extract_all(limit)` : Générateur Python qui yield les usages
- `extract_from_file(java_file)` : Extrait un fichier via API

**Extraction parallèle** :
- `FILE_WORKERS = 6` : 6 threads pour extraction des fichiers
- Queue pour résultats en temps réel
- Routing automatique par `source_file` vers DB cible

**Requête API** :
```python
POST http://localhost:8765/analyze
{
  "files": ["/absolute/path/File.java"],
  "repos": ["/path/platform", "/path/suite", "/path/modules"]
}
```

**Auto-start** : Lance JavaASTService si non démarré

### 4. AxelorXmlExtractor.py

**Rôle** : Extraction références XML Axelor

**Classe** : `AxelorXmlExtractor`

**Extrait** :
- Définitions et extensions de vues (form, grid, etc.)
- Actions (action-method, action-view, action-group)
- Triggers d'événements (onClick, onChange, etc.)
- Références de champs avec résolution de modèle
- Expressions et scripts Groovy
- Références de vues (form-view, grid-view, etc.)
- Inline action-groups (onClick="action1,action2")

**Extraction parallèle** :
- `FILE_WORKERS = 2` : 2 threads (XML files ont beaucoup d'entries)
- Queue pour résultats en temps réel
- Routing automatique par `source_file`

**Résolution de modèle** :
```python
# Hiérarchie de résolution pour les champs
1. field's target attribute
2. parent panel-related's target attribute
3. parent panel-related's grid-view/form-view (via cache)
4. parent editor's parent field target
5. parent view's model attribute (form, grid, etc.)
```

**Cache view-to-model** :
- Chargement optionnel d'un cache global `.view-model-cache.json`
- Résolution cross-module des champs dans panel-related

### 5. StorageWriter.py

**Rôle** : Interface ChromaDB pour écriture

**Classe** : `StorageWriter`

**Méthodes** :
- `add_usages(entries)` : Ajoute usages Java (batch)
- `add_xml_references(entries)` : Ajoute références XML (batch)
- `add_ts_usages(entries)` : Ajoute usages TypeScript (batch)
- `copy_from_cache(cache_path, limit)` : Copie depuis un cache DB
- `get_stats()` : Statistiques DB
- `reset()` : Vide la collection

**Modes** :
- `use_embeddings=False` : DB rapide (métadonnées uniquement)
  - Vecteurs minimaux [0.0] pour éviter le calcul d'embeddings
  - Marquage: `embedding_model_name = "none"`
- `use_embeddings=True` : DB sémantique (recherche par similarité)
  - Utilise sentence-transformers (all-MiniLM-L6-v2)
  - Calcul automatique des embeddings

**Métadonnées de tracking** :
```python
{
  "embedding_model_name": "all-MiniLM-L6-v2" ou "none",
  "document_strategy_version": "1.0" ou "none",
  "embedding_timestamp": "2025-01-15T10:30:00",
  "scan_timestamp": "2025-01-15T10:30:00"
}
```

### 6. StorageReader.py

**Rôle** : Interface ChromaDB pour lecture/requêtes

**Classe** : `StorageReader`

**Méthodes principales** :
- `query_usages(symbol, filters)` : Requête par symbole
- `search_by_file(file_path)` : Usages dans un fichier
- `get_definition(symbol)` : Trouve définitions
- `find_callers(symbol)` : Qui appelle ce symbole
- `find_callees(symbol)` : Que appelle ce symbole

### 7. JavaASTService (Java/Gradle)

**Rôle** : Service HTTP REST pour analyse AST Java

**Technologie** :
- JavaParser + JavaSymbolSolver
- Spring Boot / Javalin HTTP server
- Port 8765

**Endpoints** :
- `GET /health` : Vérification état
- `POST /analyze` : Analyse fichiers Java

**Types d'usages extraits** :
1. `java_method_call` : Appels de méthodes
2. `java_constructor_call` : new Classe()
3. `java_field_access` : Accès à des champs
4. `java_extends` : Héritage de classes
5. `java_implements` : Implémentation d'interfaces
6. `java_method_definition` : Définitions de méthodes
7. `java_class_definition` : Définitions de classes
8. `java_field_definition` : Définitions de champs

**Cache de parsers** :
- Un parser par combinaison unique de repos
- Clé de cache: Set<String> des chemins de repos
- Résolution FQN optimale avec tous les repos

## Types de bases de données

### .vector-raw-db (par défaut)

**Contenu** :
- Documents : `"usage_type: symbol in context"`
- Métadonnées : Tous les champs (file, line, FQN, etc.)
- Embeddings : **Désactivés** (vecteurs minimaux)

**Performance** :
- Insertion : ~2000 usages/sec
- Requête : Métadonnées uniquement (exact match)

**Utilisation** :
```python
manager = ExtractionManager(project_root)
manager.extract_full(use_embeddings=False)  # Par défaut
```

### .vector-semantic-db (optionnel)

**Contenu** :
- Documents : Même texte
- Métadonnées : Même structure
- Embeddings : **Activés** (all-MiniLM-L6-v2)

**Performance** :
- Insertion : ~200 usages/sec (10x plus lent)
- Requête : Recherche sémantique + métadonnées

**Utilisation** :
```python
manager.extract_full(use_embeddings=True)
```

**Cas d'usage** :
- Recherche par similarité sémantique
- "Trouve les méthodes similaires à X"
- Clustering de code

## Commandes

### Extraction complète

```bash
cd Extracteurs
python ExtractionManager.py --project-root "/path/to/project" --full
```

**Options** :
- `--full` : Extraction complète (Axelor + local)
- `--local` : Extraction locale uniquement
- `--reset true|false` : Reset database (défaut: true)
- `--with-embeddings` : Active les embeddings sémantiques
- `--limit N` : Limite d'entries par repo (pour tests)
- `--debug true|false` : Active statistiques détaillées

**Résultat** :
- Download Axelor repos (si manquant)
- Crée caches Axelor (si manquant)
- Copie caches → projet
- Extrait local → projet
- DB finale : `projet/.vector-raw-db/`

### Extraction locale

```bash
python ExtractionManager.py --project-root "/path/to/project" --local
```

**Résultat** :
- Extrait seulement modules/
- DB finale : `projet/.vector-raw-db/`

### Avec embeddings

```bash
python ExtractionManager.py --project-root "/path/to/project" --full --with-embeddings
```

**Résultat** :
- DB finale : `projet/.vector-semantic-db/`
- Temps d'extraction : ~10x plus long
- Permet recherche sémantique

### Mode debug

```bash
python ExtractionManager.py --project-root "/path/to/project" --full --debug true
```

**Résultat** :
- Affiche statistiques détaillées par repo
- Compare extracted vs stored entries
- Montre les ratios d'expansion (bidirectional refs)

## Intégration MCP

### Serveur MCP

**Fichier** : `../mcp_callgraph_server.py`

**Démarrage** :
```json
// claude_desktop_config.json
{
  "mcpServers": {
    "callgraph": {
      "command": "python",
      "args": ["C:/path/to/mcp_callgraph_server.py"]
    }
  }
}
```

**Auto-détection DB** :
```python
cwd = Path.cwd()  # Répertoire du projet courant

if (cwd / ".vector-semantic-db").exists():
    db = cwd / ".vector-semantic-db"
elif (cwd / ".vector-raw-db").exists():
    db = cwd / ".vector-raw-db"
```

**Outils MCP** :
- `find_usages(symbol, depth, filters)` : Trouve usages
- `get_definition(symbol)` : Trouve définition
- `find_callers(symbol)` : Qui appelle
- `find_callees(symbol)` : Qu'appelle
- `impact_analysis(symbol, depth)` : Impact récursif
- `search_by_file(file_path)` : Usages dans un fichier
- `get_stats(module)` : Statistiques
- `extract(mode, reset)` : Lance extraction

## Performance

### Extraction complète (projet type)

**Configuration** :
- Platform : ~800 fichiers Java
- Suite : ~3,500 fichiers Java
- Local : ~2,700 fichiers Java
- Total : ~7,000 fichiers

**Temps (sans cache)** :
- Download Axelor : ~2 min (première fois)
- Extraction platform : ~8 min
- Extraction suite : ~15 min
- Extraction local : ~10 min
- **Total : ~35 min**

**Temps (avec cache)** :
- Cache hit platform : 0s
- Cache hit suite : 0s
- Copie caches : ~5s
- Extraction local : ~10 min
- **Total : ~10 min** (gain 70%)

**Parser cache** :
- Création parser {platform, suite} : ~4s
- Réutilisation : 0ms
- Création parser {platform, suite, modules} : ~5s

### Taille des bases

**Platform v8.2.x** :
- .vector-raw-db : ~50 MB
- Usages : ~90,000

**Suite v8.2.x** :
- .vector-raw-db : ~120 MB
- Usages : ~210,000

**Local (projet type)** :
- .vector-raw-db : ~80 MB
- Usages : ~160,000

**Total projet** :
- .vector-raw-db : ~250 MB
- Usages : ~460,000

## Troubleshooting

### JavaASTService ne démarre pas

**Symptômes** :
```
Error: JavaASTService did not start within 30 seconds
```

**Solutions** :
1. Vérifier Java installé : `java -version` (besoin Java 11+)
2. Builder le service : `cd JavaASTService && gradlew build`
3. Démarrer manuellement : `gradlew service`

### Cache non détecté

**Symptômes** :
```
[CACHE] No cached DB for platform v8.2.x, will extract
```

**Vérification** :
```bash
ls axelor-repos/axelor-open-platform-8.2.x/.vector-raw-db/
# Doit contenir : chroma.sqlite3
```

**Solution** :
- Réextraire : `--full --reset true`

### Résolution FQN échoue

**Symptômes** :
```
calleeFqn: "unknown"
```

**Cause** : Repos manquants dans JavaASTService

**Solution** :
- Vérifier que tous les repos sont passés dans `repos=[...]`
- Le service doit avoir accès aux sources pour résolution

### Extraction lente

**Optimisations** :
1. Utiliser mode rapide (pas d'embeddings - défaut)
2. Réutiliser les caches Axelor (`--reset false`)
3. Garder JavaASTService en mémoire (pas de redémarrage)

### Erreurs de routing

**Symptômes** :
```
[DEBUG] Entry X NOT MATCHED
```

**Causes** :
- Chemins relatifs vs absolus
- Séparateurs Windows vs Unix

**Solution** :
- Tous les chemins sont normalisés (.resolve() + replace('\\', '/'))
- Si le problème persiste, activer `--debug true` pour voir les chemins

## Développement

### Ajouter un nouveau type d'usage Java

1. **JavaASTService** : Modifier `UsageCollector.java`
2. **Extraction** : Aucune modification (automatique)
3. **DB** : Aucune modification (métadonnées flexibles)
4. **MCP** : Aucune modification (requêtes génériques)

### Ajouter un nouveau type d'extracteur

1. Créer `XXXExtractor.py` avec générateur `extract_all()`
2. Implémenter :
   - `discover_files(repo)` : Liste les fichiers
   - `extract_from_file(file, queue)` : Extrait un fichier
   - `extract_all(limit)` : Générateur avec parallélisation
3. Ajouter appel dans `ExtractionManager._extract_all_repos()`

**Pattern à suivre** :
```python
class XXXExtractor:
    FILE_WORKERS = 4  # Nombre de threads

    def __init__(self, repos: List[str]):
        self.repos = repos

    def extract_all(self, limit: Optional[int] = None) -> Iterator[Tuple[str, Dict]]:
        # Découverte fichiers
        # Extraction parallèle
        # Yield ('xxx', entry_dict)
        pass
```

### Tester l'extraction

```bash
# Test sur un seul fichier
cd Extracteurs
python JavaASTExtractor.py path/to/File.java

# Test sur un module avec limite
python ExtractionManager.py --project-root . --full --limit 100

# Stats DB
python -c "
from StorageWriter import StorageWriter
db = StorageWriter('.vector-raw-db')
print(db.get_stats())
"
```

## Architecture v2.0 - Changements majeurs

### Avant (v1.0)

```
Pour chaque repo:
  1. Extrait repo → DB temporaire
  2. Copie DB temporaire → DB finale

→ Problème: Extraction séquentielle, lente
→ N extractions pour N repos
```

### Après (v2.0)

```
Une seule extraction pour tous les repos:
  1. Découvre fichiers de TOUS les repos
  2. Extrait en parallèle avec routing par source_file
  3. Entries routées automatiquement vers DB cible

→ Avantage: Extraction parallèle, rapide
→ 1 extraction pour N repos
→ Résolution FQN complète pour tous
```

### Gains de performance v2.0

- Temps d'extraction : -40% (extraction parallèle)
- Résolution FQN : 100% (tous repos dès le départ)
- Mémoire : -50% (pas de DB temporaires)
- Code : -30% (moins de duplication)

## Formats de données

### Entry format (standardisé)

```python
{
  "document": "usage_type: symbol in context",  # Pour embeddings
  "metadata": {
    "source": "java" | "xml" | "typescript",
    "source_file": "/absolute/path/to/file",
    "usageType": "java_method_call" | "xml_trigger_calls_action" | ...,
    "calleeSymbol": "methodName" | "actionName" | "fieldName",
    "callerSymbol": "methodName" | "viewId:fieldName:onClick",
    "callerUri": "/absolute/path/to/caller",
    "callerLine": 42,
    "calleeFqn": "com.example.Class.method",
    "module": "module-name",
    # ... autres champs spécifiques au type
  }
}
```

### Métadonnées de tracking

```python
{
  "embedding_model_name": "all-MiniLM-L6-v2" | "none",
  "document_strategy_version": "1.0" | "none",
  "embedding_timestamp": "2025-01-15T10:30:00",
  "scan_timestamp": "2025-01-15T10:30:00"
}
```

## Scripts utilitaires

### Regénérer les embeddings

```bash
python scripts/regenerate_embeddings.py
```

**Utilité** :
- Convertir DB rapide → DB sémantique
- Mettre à jour embeddings obsolètes
- Traitement par batches pour grandes DBs

### Nettoyer les caches

```bash
# Supprimer tous les caches Axelor
rm -rf axelor-repos/*/. vector-*-db/

# Supprimer cache d'une version spécifique
rm -rf axelor-repos/axelor-open-platform-8.2.9/.vector-raw-db/
```

### Vérifier la santé de la DB

```bash
python StorageWriter.py --health
```

**Affiche** :
- Nombre total d'entries
- Pourcentage avec embeddings
- Entries avec stratégie obsolète
- Recommandations

## Références

- [JavaParser Documentation](https://javaparser.org/)
- [ChromaDB Documentation](https://docs.trychroma.com/)
- [Axelor Developer Guide](https://docs.axelor.com/)
- [MCP Protocol Specification](https://modelcontextprotocol.io/)
