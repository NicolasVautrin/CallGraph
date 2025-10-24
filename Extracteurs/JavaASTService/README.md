# JavaASTService

Service HTTP pour l'extraction d'usages Java avec résolution de types et cache de parsers.

## Fonctionnalités

- **Extraction d'usages Java** : Analyse AST avec JavaParser pour détecter méthodes, constructeurs, champs, héritage
- **Résolution de types** : JavaSymbolSolver pour obtenir les FQN (Fully Qualified Names)
- **Cache de parsers** : Réutilisation intelligente des TypeSolver par ensemble de repos
- **Multi-repositories** : Support de plusieurs repos pour résolution cross-projet

## Installation

```bash
cd JavaASTService
./gradlew build
```

## Démarrage

```bash
./gradlew service
# ou
java -jar build/libs/JavaASTService.jar [port]
```

Port par défaut : **8765**

## API

### POST /analyze

Analyse des fichiers Java et extraction des usages.

**Request Body** :
```json
{
  "files": [
    "/absolute/path/to/File1.java",
    "/absolute/path/to/File2.java"
  ],
  "repos": [
    "/path/to/repo1",
    "/path/to/repo2"
  ]
}
```

**Paramètres** :
- `files` (required) : Liste de chemins absolus vers les fichiers Java à analyser
- `repos` (optional) : Liste de repos pour la résolution de types
  - Si fourni : crée/réutilise un TypeSolver pour ces repos
  - Si omis : utilise le parser global configuré dans `config.json`

**Response** :
```json
{
  "processed": 2,
  "failed": 0,
  "elapsed_ms": 1234,
  "results": [
    {
      "success": true,
      "file": "/absolute/path/to/File1.java",
      "package": "com.example",
      "module": "my-module",
      "usages": [
        {
          "usageType": "java_method_call",
          "calleeSymbol": "doSomething",
          "calleeFqn": "com.example.Service.doSomething",
          "callerSymbol": "myMethod",
          "callerFqn": "com.example.Controller.myMethod",
          "line": 42,
          "source_file": "/absolute/path/to/File1.java",
          "module": "my-module"
        }
      ],
      "usage_count": 1
    }
  ]
}
```

### GET /health

Health check.

**Response** :
```json
{
  "status": "ok",
  "service": "JavaASTService"
}
```

### POST /shutdown

Arrêt gracieux du service.

**Response** :
```json
{
  "status": "shutting down"
}
```

## Cache de Parsers

Le service maintient un cache de `JavaParser` indexé par ensemble de repositories.

**Principe** :
1. Premier appel avec `repos=[A, B, C]` :
   - Crée un `CombinedTypeSolver` avec A, B, C
   - Scanne les `src/main/java` et `build/src-gen/java` de chaque repo
   - Crée un `JavaParser` avec ce `TypeSolver`
   - **Met en cache** avec la clé `{A, B, C}`

2. Appels suivants avec `repos=[A, B, C]` :
   - Détecte que `{A, B, C}` existe dans le cache
   - **Réutilise le parser** (pas de recréation du TypeSolver)

3. Appel avec `repos=[A, B]` (différent) :
   - Crée un nouveau parser pour `{A, B}`
   - Met en cache séparément

**Avantages** :
- Évite de recréer les TypeSolver (opération coûteuse)
- Résolution FQN optimale avec le bon contexte
- Efficace en mémoire : même repos = même parser

**Logs du service** :
```
[NEW] Creating parser for 2 repos:
  - C:/path/to/platform
  - C:/path/to/suite
  Found 45 Java source directories
  OK - Parser cached

[CACHE] Using cached parser for 2 repos
```

## Cas d'usage : Extraction avec cache

### Scénario : Extraction Axelor platform + suite + modules

**Extraction 1 : Axelor platform seul**
```json
POST /analyze {
  "files": ["platform/src/main/java/..."],
  "repos": [
    "C:/axelor-repos/axelor-open-platform-8.2.9",
    "C:/axelor-repos/axelor-open-suite-8.2.9"
  ]
}
```
→ Logs : `[NEW] Creating parser for 2 repos` (3-5 secondes)
→ Analyse les fichiers platform
→ Met en cache le parser pour `{platform, suite}`

**Extraction 2 : Axelor suite seul**
```json
POST /analyze {
  "files": ["suite/src/main/java/..."],
  "repos": [
    "C:/axelor-repos/axelor-open-platform-8.2.9",
    "C:/axelor-repos/axelor-open-suite-8.2.9"
  ]
}
```
→ Logs : `[CACHE] Using cached parser for 2 repos` (0ms)
→ Analyse les fichiers suite
→ Réutilise le parser (gain de temps)

**Extraction 3 : Modules locaux**
```json
POST /analyze {
  "files": ["modules/open-auction-base/src/..."],
  "repos": [
    "C:/axelor-repos/axelor-open-platform-8.2.9",
    "C:/axelor-repos/axelor-open-suite-8.2.9",
    "C:/Bricklead_Encheres/modules"
  ]
}
```
→ Logs : `[NEW] Creating parser for 3 repos` (4-6 secondes)
→ Analyse les fichiers locaux
→ Nouveau parser car ensemble de repos différent

**Résultat** :
- Sans cache : ~15 secondes (3 créations de parser)
- Avec cache : ~7 secondes (2 créations + 1 réutilisation)

## Filtrage côté Python

Le service renvoie **tous les usages** des fichiers analysés. Le filtrage par `source_file` se fait côté Python.

**Exemple dans extraction_manager.py** :
```python
# Extraire platform seul avec résolution complète
java_extractor = JavaCallGraphExtractor(repos=[platform_path, suite_path])

for source_type, entry in java_extractor.extract_all():
    # Filtrer : garder seulement les fichiers de platform
    source_file = entry['metadata']['source_file']
    if source_file.startswith(str(platform_path)):
        cache_db.add_usages([entry])
```

**Avantages** :
- Le service garde son cache de parser
- La résolution FQN est correcte (tous les repos disponibles)
- Chaque repo Axelor a son propre cache de DB
- Le projet final fusionne tous les caches

## Configuration

### config.json (optionnel)

Si `repos` n'est pas fourni dans la requête, le service utilise `config.json` :

```json
{
  "repositories": [
    "/path/to/repo1",
    "/path/to/repo2"
  ],
  "domain_patterns": [
    "com.axelor.*",
    "com.mycompany.*"
  ]
}
```

**Paramètres** :
- `repositories` : Repos utilisés par le parser global (si `repos` non fourni)
- `domain_patterns` : Patterns pour filtrer les usages (optionnel)

## Types d'usages extraits

Le service détecte 8 types d'usages Java :

1. **java_method_call** : Appel de méthode
2. **java_constructor_call** : Appel de constructeur
3. **java_field_access** : Accès à un champ
4. **java_static_import** : Import statique
5. **java_extends** : Héritage de classe
6. **java_implements** : Implémentation d'interface
7. **java_method_declaration** : Déclaration de méthode
8. **java_field_declaration** : Déclaration de champ

Chaque usage contient :
- `calleeSymbol` : Nom court du symbole utilisé
- `calleeFqn` : Nom complet (Fully Qualified Name)
- `callerSymbol` : Méthode/classe appelante
- `callerFqn` : FQN de l'appelant
- `line` : Numéro de ligne
- `source_file` : Fichier source
- `module` : Module extrait du chemin

## Intégration Python

Voir `extract_java_graph.py` :

```python
from extract_java_graph import JavaCallGraphExtractor

# Extraction avec repos spécifiques
extractor = JavaCallGraphExtractor(
    repos=["/path/to/platform", "/path/to/suite", "/path/to/modules"]
)

for source_type, entry in extractor.extract_all():
    print(f"{entry['document']}")
    # entry['metadata'] contient tous les détails
```

Le service démarre automatiquement si nécessaire.

## Architecture

```
JavaASTService
├── JavaParser : Parser Java avec symboles
│   └── ParserConfiguration
│       └── JavaSymbolSolver
│           └── CombinedTypeSolver
│               ├── ReflectionTypeSolver (JDK)
│               └── JavaParserTypeSolver × N (repos)
│
└── Parser Cache : Map<Set<String>, JavaParser>
    ├── {repo1, repo2} → JavaParser instance 1
    ├── {repo1, repo2, repo3} → JavaParser instance 2
    └── ...
```

## Performance

- **Premier appel** avec N repos : ~3-5s (création TypeSolver + scan des sources)
- **Appels suivants** avec les mêmes repos : ~0ms (cache hit)
- **Analyse d'un fichier** : ~50-200ms selon la taille

**Optimisation** :
- Grouper les extractions par ensemble de repos identique
- Le service reste en mémoire (pas de redémarrage entre extractions)
- Maximiser les cache hits en utilisant le même ordre de repos
