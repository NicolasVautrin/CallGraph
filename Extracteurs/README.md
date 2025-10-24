# Extracteurs - Architecture du système d'extraction

Système d'extraction du call graph Java/XML pour projets Axelor avec cache intelligent.

## Vue d'ensemble

Le système extrait les relations entre symboles (méthodes, classes, champs) depuis :
- **Projets Axelor locaux** (modules/)
- **Dépendances Axelor** (axelor-open-platform, axelor-open-suite)

Et stocke les résultats dans une **base vectorielle ChromaDB** pour requêtes rapides via le serveur MCP.

## Architecture globale

```
┌─────────────────────────────────────────────────────────────────┐
│                     MCP CallGraph Server                        │
│  (mcp_callgraph_server.py)                                      │
│  - API MCP pour interroger le call graph                        │
│  - Auto-détecte .vector-raw-db ou .vector-semantic-db           │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                 Extraction Manager                              │
│  (extraction_manager.py)                                        │
│  - Orchestration des extractions                                │
│  - Gestion du cache Axelor                                      │
│  - Fusion des bases de données                                  │
└──────┬──────────────────────────────┬───────────────────────────┘
       │                              │
       ▼                              ▼
┌──────────────────┐          ┌──────────────────────┐
│ Axelor Repo Mgr  │          │  CallGraph DB        │
│ (fetch_axelor_   │          │  (build_call_graph_  │
│  repos.py)       │          │   db.py)             │
│ - Détecte        │          │ - Interface ChromaDB │
│   versions       │          │ - Indexation usages  │
│ - Download repos │          │ - Requêtes           │
│ - Gère caches DB │          └──────────────────────┘
└────────┬─────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Extracteurs                             │
├──────────────────────────────┬──────────────────────────────────┤
│  Java Extractor              │  XML Extractor                   │
│  (extract_java_graph.py)     │  (extract_xml_graph.py)          │
│  - Découvre fichiers .java   │  - Découvre fichiers .xml        │
│  - Envoie à JavaASTService   │  - Parse XML Axelor              │
│  - Filtre résultats          │  - Extrait actions/views/menus   │
│  - Générateur Python         │  - Générateur Python             │
└────────┬─────────────────────┴──────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                   JavaASTService (Java)                         │
│  - Service HTTP REST (port 8765)                                │
│  - JavaParser + JavaSymbolSolver                                │
│  - Cache de parsers par ensemble de repos                       │
│  - Résolution FQN et emplacements                               │
└─────────────────────────────────────────────────────────────────┘
```

## Flux d'extraction

### Mode "full" : Extraction complète avec Axelor

```
1. Détection versions
   extraction_manager.py
   └─> fetch_axelor_repos.py : detect_axelor_versions()
       ├─> Lit gradle.properties
       └─> Trouve platform=8.2.9, suite=8.2.9

2. Download Axelor repos (si nécessaire)
   fetch_axelor_repos.py : ensure_repos()
   └─> git clone axelor-open-platform v8.2.9
   └─> git clone axelor-open-suite v8.2.9
       Destination: axelor-repos/axelor-open-platform-8.2.9/
                   axelor-repos/axelor-open-suite-8.2.9/

3. Vérification des caches
   fetch_axelor_repos.py : has_cached_db()
   ├─> Cherche axelor-open-platform-8.2.9/.vector-raw-db/
   ├─> Cherche axelor-open-suite-8.2.9/.vector-raw-db/
   └─> Si manquant → extraction nécessaire

4. Extraction des repos manquants (avec cache de parser)
   Pour chaque repo Axelor manquant :

   extraction_manager.py : _extract_repo_to_cache()
   ├─> JavaCallGraphExtractor(repos=[platform, suite])
   │   └─> extract_java_graph.py
   │       └─> POST /analyze avec repos=[platform, suite]
   │           └─> JavaASTService
   │               ├─> [NEW] Creating parser for 2 repos (premier appel)
   │               ├─> Scanne src/main/java de chaque repo
   │               └─> Cache le parser avec clé {platform, suite}
   │
   ├─> Filtre les résultats par source_file
   │   (garde seulement les fichiers du repo en cours)
   │
   └─> Sauvegarde dans cache
       platform/.vector-raw-db/  (usages de platform uniquement)
       suite/.vector-raw-db/     (usages de suite uniquement)

5. Construction de la base projet
   extraction_manager.py : extract_full()
   ├─> Copie platform/.vector-raw-db/ → projet/.vector-raw-db/
   ├─> Fusionne suite/.vector-raw-db/ (si multiple repos)
   │   └─> Réutilise cache parser {platform, suite}
   │
   └─> Extraction modules locaux
       ├─> JavaCallGraphExtractor(repos=[platform, suite, modules])
       │   └─> POST /analyze avec repos=[platform, suite, modules]
       │       └─> JavaASTService
       │           └─> [NEW] Creating parser for 3 repos (nouveau set)
       │
       ├─> Filtre par modules/
       └─> Ajoute à projet/.vector-raw-db/

6. Résultat final
   projet/.vector-raw-db/
   ├─> Usages de platform (copiés du cache)
   ├─> Usages de suite (copiés du cache)
   └─> Usages de modules (extraits avec résolution complète)
```

### Mode "local" : Extraction modules uniquement

```
1. Extraction locale
   extraction_manager.py : extract_local()
   └─> JavaCallGraphExtractor(repos=[modules])
       └─> POST /analyze avec repos=[modules]
           └─> JavaASTService
               └─> [NEW] Creating parser for 1 repo

   Note: Résolution FQN limitée (seulement modules/)
```

## Stratégie de cache

### Cache de bases vectorielles

**Localisation** :
```
axelor-repos/
├─ axelor-open-platform-8.2.9/
│  ├─ .vector-raw-db/         # Cache DB pour platform
│  └─ .vector-semantic-db/    # Cache DB avec embeddings (optionnel)
└─ axelor-open-suite-8.2.9/
   ├─ .vector-raw-db/         # Cache DB pour suite
   └─ .vector-semantic-db/    # Cache DB avec embeddings (optionnel)

projet/
└─ .vector-raw-db/            # DB finale (Axelor + local)
   ou .vector-semantic-db/
```

**Avantages** :
- Extraction Axelor : une fois par version
- Projets multiples : réutilisation des caches
- Gain de temps : 90% sur re-extraction

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

## Composants détaillés

### 1. extraction_manager.py

**Rôle** : Orchestration de l'extraction complète

**Méthodes principales** :
- `ensure_axelor_repos()` : Download repos Axelor si nécessaire
- `extract_full(reset, use_embeddings)` : Extraction complète avec cache
- `extract_local(use_embeddings)` : Extraction locale uniquement
- `_extract_repo_to_cache(repo, cache_db, all_repos)` : Extrait un repo dans son cache
- `_extract_repo_into_db(repo, db, all_repos)` : Extrait un repo dans une DB existante

**Flux extract_full()** :
1. Détecte versions Axelor
2. Download repos manquants
3. Vérifie caches existants
4. Extrait repos manquants → caches
5. Copie caches → projet DB
6. Extrait local → projet DB

### 2. fetch_axelor_repos.py

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

**Download** :
```bash
git clone --branch v8.2.9 --depth 1 \
  https://github.com/axelor/axelor-open-platform.git \
  axelor-repos/axelor-open-platform-8.2.9
```

### 3. extract_java_graph.py

**Rôle** : Extraction usages Java via JavaASTService

**Classe** : `JavaCallGraphExtractor`

**Méthodes** :
- `__init__(repos)` : Initialise avec liste de repos
- `discover_java_files()` : Trouve tous les .java (exclut build/, node_modules/)
- `extract_all(limit)` : Générateur Python qui yield les usages
- `extract_from_file(java_file)` : Extrait un fichier via API

**Requête API** :
```python
POST http://localhost:8765/analyze
{
  "files": ["/absolute/path/File.java"],
  "repos": ["/path/platform", "/path/suite", "/path/modules"]
}
```

**Auto-start** : Lance JavaASTService si non démarré

### 4. extract_xml_graph.py

**Rôle** : Extraction références XML Axelor

**Classe** : `AxelorXmlExtractor`

**Extrait** :
- Actions (action-method, action-view, etc.)
- Vues (form, grid, etc.)
- Menus
- Appels de méthodes depuis XML

**Générateur** : Même pattern que JavaCallGraphExtractor

### 5. build_call_graph_db.py

**Rôle** : Interface ChromaDB

**Classe** : `CallGraphDB`

**Méthodes** :
- `add_usages(entries)` : Ajoute usages Java (batch)
- `add_xml_references(entries)` : Ajoute références XML (batch)
- `query_usages(symbol, filters)` : Requête par symbole
- `get_stats()` : Statistiques DB
- `reset()` : Vide la collection

**Modes** :
- `use_embeddings=False` : DB rapide (métadonnées uniquement)
- `use_embeddings=True` : DB sémantique (recherche par similarité)

### 6. call_graph_service.py

**Rôle** : Requêtes avancées sur le call graph

**Méthodes** :
- `find_usages(symbol, depth, filters)` : Trouve usages avec récursion
- `get_definition(symbol)` : Trouve définitions
- `find_callers(symbol)` : Qui appelle ce symbole
- `find_callees(symbol)` : Que appelle ce symbole
- `impact_analysis(symbol, depth)` : Analyse d'impact récursive
- `format_result(operation, data)` : Formatage pretty print

## Types de bases de données

### .vector-raw-db (par défaut)

**Contenu** :
- Documents : `"java_method_call: doSomething in myMethod() at com.example.Controller"`
- Métadonnées : Tous les champs (file, line, FQN, etc.)
- Embeddings : **Désactivés**

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
python extraction_manager.py --project-root "/path/to/project" --full
```

**Résultat** :
- Download Axelor repos (si manquant)
- Crée caches Axelor (si manquant)
- Copie caches → projet
- Extrait local → projet
- DB finale : `projet/.vector-raw-db/`

### Extraction locale

```bash
python extraction_manager.py --project-root "/path/to/project" --local
```

**Résultat** :
- Extrait seulement modules/
- DB finale : `projet/.vector-raw-db/`

### Avec embeddings

```bash
python extraction_manager.py --project-root "/path/to/project" --full --with-embeddings
```

**Résultat** :
- DB finale : `projet/.vector-semantic-db/`

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
      "args": ["C:/Users/nicolasv/MCP_servers/CallGraph/mcp_callgraph_server.py"]
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

## Performance

### Extraction complète (projet Bricklead)

**Configuration** :
- Platform : 829 fichiers Java
- Suite : 3,467 fichiers Java
- Local : 2,729 fichiers Java
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

**Platform 8.2.9** :
- .vector-raw-db : ~50 MB
- Usages : ~90,000

**Suite 8.2.9** :
- .vector-raw-db : ~120 MB
- Usages : ~210,000

**Local (Bricklead)** :
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
2. Builder le service : `cd JavaASTService && ./gradlew build`
3. Démarrer manuellement : `./gradlew service`

### Cache non détecté

**Symptômes** :
```
[CACHE] No cached DB for platform v8.2.9, will extract
```

**Vérification** :
```bash
ls axelor-repos/axelor-open-platform-8.2.9/.vector-raw-db/
# Doit contenir : chroma.sqlite3
```

**Solution** :
- Réextraire : `--full --reset`

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
1. Utiliser `--no-embeddings` (10x plus rapide)
2. Réutiliser les caches Axelor (pas de `--reset`)
3. Garder JavaASTService en mémoire (pas de redémarrage)

## Développement

### Ajouter un nouveau type d'usage

1. **JavaASTService** : Modifier `UsageCollector.java`
2. **Extraction** : Aucune modification nécessaire (automatique)
3. **DB** : Aucune modification (métadonnées flexibles)
4. **MCP** : Aucune modification (requêtes génériques)

### Ajouter un nouveau langage

1. Créer `extract_XXX_graph.py` avec générateur
2. Ajouter appel dans `extraction_manager.py`
3. Suivre le pattern Java/XML

### Tester l'extraction

```bash
# Test sur un seul fichier
cd Extracteurs
python extract_java_graph.py path/to/File.java

# Test sur un module
python extract_java_graph.py modules/open-auction-base/

# Stats
python -c "
from build_call_graph_db import CallGraphDB
db = CallGraphDB('.vector-raw-db')
print(db.get_stats())
"
```
