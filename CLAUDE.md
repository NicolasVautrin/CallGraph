# CallGraph - MCP Server pour Analyse de Code Axelor

## Vue d'ensemble

Serveur MCP qui analyse les graphes d'appels Java dans les projets Axelor via bytecode ASM.
- **Extraction** : ASM (bytecode) → SQLite
- **Découverte** : Gradle auto-détecte les dépendances Axelor
- **Requête** : MCP tools + requêtes SQL directes
- **Cache** : SHA256-based pour packages Axelor

## Architecture

```
Projet Axelor (.callgraph.db)
    ↓ Extraction via
run_asm_extraction.py
    ↓ Orchestration
├─> GradleDependencyManager (Gradle discovery)
└─> ASMExtractor (Python client)
    ↓ Analyse bytecode
ASMAnalysisService (Java service, port 8766)
    ↓ ASM ClassVisitor
├─> symbol_index (FQN → URI)
├─> nodes (classes, methods)
└─> edges (calls, inheritance, member_of)
```

## Règles critiques pour l'analyse de code

### ⚠️ TOUJOURS utiliser les outils MCP SQLite

Quand l'utilisateur demande :
- "Où est utilisée cette méthode ?"
- "Quelles sont les dépendances ?"
- "Qui appelle cette fonction ?"
- "Méthodes utilisant la classe X comme argument ?"

**Utiliser OBLIGATOIREMENT** :
- Les outils SQLite MCP pour requêtes SQL (`read_query`, `list_tables`, `describe_table`)
- Le nom exact des outils dépend du nom du serveur MCP configuré

**NE JAMAIS** :
- Utiliser Grep/Read pour trouver des usages de méthodes
- Scanner manuellement les fichiers Java
- Deviner les relations entre classes

### Pourquoi ?

CallGraph utilise **ASM bytecode analysis** et a déjà indexé :
- Tous les appels de méthodes (100% précis, depuis bytecode)
- Relations d'héritage et d'implémentation
- Types d'arguments, retours, champs
- FQN résolus pour toutes les dépendances Axelor

## Base de données SQLite

### Tables principales

**`symbol_index`** : FQN → URI → package
```sql
fqn TEXT PRIMARY KEY           -- com.axelor.db.Model
uri TEXT                       -- file:///path/to/Model.java (ou file:///.../Model.java:42 pour méthodes)
package TEXT                   -- axelor-core-7.2.6
line INTEGER                   -- Numéro de ligne (pour méthodes uniquement)
```

**`nodes`** : Classes et méthodes
```sql
fqn TEXT PRIMARY KEY           -- com.axelor.db.Model.getId()
type TEXT                      -- 'class', 'interface', 'enum', 'method'
package TEXT                   -- axelor-core-7.2.6
line INTEGER
visibility TEXT                -- 'public', 'private', 'protected', 'package'
has_override BOOLEAN           -- TRUE si @Override (méthodes uniquement)
is_transactional BOOLEAN       -- TRUE si @Transactional (méthodes uniquement)
```

**`edges`** : Relations
```sql
from_fqn TEXT                  -- Source
edge_type TEXT                 -- 'call', 'inheritance', 'member_of'
to_fqn TEXT                    -- Target
kind TEXT                      -- 'invoke', 'extends', 'implements', 'argument', 'return'
from_package TEXT              -- Package source
to_package TEXT                -- Package target
from_line INTEGER
```

### Exemples de requêtes

**Trouver méthodes utilisant classe X comme argument** :
```sql
SELECT DISTINCT e.to_fqn AS method_fqn, e.from_fqn AS argument_type
FROM edges e
WHERE e.edge_type = 'member_of' AND e.kind = 'argument'
  AND e.from_fqn = 'com.axelor.apps.openauction.db.Lot'
ORDER BY e.to_fqn;
```

**Trouver tous les appels à une méthode** :
```sql
SELECT e.from_fqn AS caller, e.to_fqn AS callee, e.from_line, e.from_package
FROM edges e
WHERE e.edge_type = 'call' AND e.to_fqn LIKE '%setStatus%'
ORDER BY e.from_package;
```

**Compter symboles par package** :
```sql
SELECT package, COUNT(*) as symbol_count,
       SUM(CASE WHEN type = 'class' THEN 1 ELSE 0 END) as class_count,
       SUM(CASE WHEN type = 'method' THEN 1 ELSE 0 END) as method_count
FROM nodes
GROUP BY package ORDER BY symbol_count DESC;
```

**Trouver classes héritant de X** :
```sql
SELECT DISTINCT e.from_fqn AS subclass, e.to_fqn AS superclass
FROM edges e
WHERE e.edge_type = 'inheritance' AND e.kind = 'extends'
  AND e.to_fqn LIKE '%Model%'
ORDER BY e.from_fqn;
```

**Trouver méthodes transactionnelles** :
```sql
SELECT fqn, package, line
FROM nodes
WHERE type = 'method' AND is_transactional = 1
ORDER BY package, line;
```

**Trouver méthodes publiques avec @Override** :
```sql
SELECT fqn, package, line, visibility
FROM nodes
WHERE type = 'method' AND visibility = 'public' AND has_override = 1
ORDER BY package, line;
```

## Structure du projet

```
CallGraph/
├── run_asm_extraction.py            # CLI extraction
├── mcp_callgraph_server.py          # Serveur MCP (legacy)
├── mcp.json                         # Config MCP
├── ASM_ARCHITECTURE.md              # Architecture détaillée
├── Extracteurs/
│   ├── GradleDependencyManager.py   # Gradle discovery + JAR extraction
│   ├── ASMExtractor.py              # Python client pour ASM service
│   └── ASMAnalysisService/          # Service Java (ASM + Spark)
│       └── service/                 # Kotlin/Gradle service
└── axelor-repos/                    # Cache JARs extraits
    └── axelor-core-7.2.6/
        ├── classes/                 # .class files
        └── sources/                 # .java files (optionnel)
```

## Commandes fréquentes

⚠️ **IMPORTANT** : **TOUJOURS lancer l'extraction en background** (longue durée: 5-10 min)
- Utiliser `run_in_background=True` avec l'outil Bash
- **NE PAS mettre de timeout**
- Permet de continuer à travailler pendant l'extraction
- Vérifier la progression avec BashOutput

### Extraction incrémentale (défaut, utilise le cache)
```bash
python run_asm_extraction.py /path/to/project
```
→ Mode intelligent : ne réextrait QUE les packages modifiés (SHA256)

### Reset complet (première fois ou après changement de schéma)
```bash
python run_asm_extraction.py /path/to/project --init
```
→ Drop toutes les tables et reconstruit from scratch

### Limite pour tests (nécessite --init)
```bash
python run_asm_extraction.py /path/to/project --init --limit 100
```
⚠️ **Note** : `--limit` nécessite `--init` pour éviter données partielles en mode incrémental

**Modes** :
- **Par défaut (Incrémental)** : Cache SHA256 automatique + nettoyage ciblé
- **--init** : Reset total - utiliser pour première extraction ou après modification de schéma

### Service ASM (Java)
```bash
cd Extracteurs/ASMAnalysisService
./gradlew.bat run
```
→ Service HTTP sur port 8766
→ Logs écrits dans `asm-service.log`

**Endpoints disponibles** :
- `GET /health` - Health check
- `POST /index` - Indexation légère (fichier unique, symboles uniquement)
- `POST /index/batch` - Indexation légère (batch de fichiers, ~50-70% plus rapide)
- `POST /analyze` - Analyse complète (call graph)
- `POST /shutdown` - Arrêt propre du service

**Métadonnées extraites** :
- Visibilité des classes et méthodes (public, private, protected, package)
- Annotations : @Override, @Transactional (Spring, Jakarta, javax)
- Numéros de ligne pour méthodes
- Relations d'héritage et d'implémentation
- Appels de méthodes avec numéros de ligne

### Tuer processus Java bloqués
```bash
wmic process where "name='java.exe' and CommandLine like '%ASMAnalysisService%'" delete
```

## Outils MCP disponibles

### Serveur SQLite MCP (recommandé)
Utiliser les outils fournis par le serveur SQLite MCP configuré :
- `list_tables` : Lister les tables
- `describe_table` : Structure d'une table
- `read_query` : Requêtes SQL directes

**Note** : Le nom exact du serveur et des outils dépend de votre configuration MCP.

### callgraph legacy (obsolète, peut ne pas fonctionner)
- `find_usages`, `get_definition`, `find_callers`, etc.
- **Note** : Conçu pour ChromaDB, pas SQLite

## Avantages ASM vs JavaParser

| Critère | ASM (actuel) | JavaParser (ancien) |
|---------|--------------|---------------------|
| **Input** | Bytecode (.class) | Source (.java) |
| **Sources requises** | Non (JARs suffisent) | Oui |
| **Vitesse** | Rapide | Lent (parsing) |
| **Précision** | 100% (bytecode) | Approximative |
| **Dependencies** | Auto (Gradle) | Manuel |
| **Storage** | SQLite (50 MB) | ChromaDB (150 MB) |
| **Queries** | SQL (10ms) | Vector (50ms) |

## Cache Axelor

Packages Axelor extraits et cachés automatiquement :
```
axelor-repos/
├── axelor-core-7.2.6/
│   ├── classes/                # Bytecode
│   └── sources/                # Sources (optionnel)
└── axelor-base-8.2.9/
    ├── classes/
    └── sources/
```

**Cache invalidation** : SHA256 des .class files

## Performance

**Extraction complète (--init)** : ~6-7 min pour projet complet
- 39 packages Axelor (9,466 fichiers .class)
- 107k symboles indexés (9.4k classes + 98k méthodes)
- 319k edges (appels, héritage, member_of)
- Taille base : ~292 MB

**Détail par étape** :
- STEP 1 (découverte Gradle) : ~10s
- STEP 2 (indexation symboles) : ~6 min
- STEP 3 (extraction call graph) : ~40s (252 fichiers/sec)

**Mode incrémental** : Ne réextrait que les packages modifiés (70%+ speedup sur runs suivants)
**Mode --init** : Extraction complète from scratch (utiliser pour schema changes)

**Optimisations** :
- Requêtes SQL batchées (IN clauses) : 99.95% de réduction des requêtes
- Avant : ~650k requêtes → Après : ~220 requêtes
- Insertion par batches de 5000 lignes
- Index SQLite sur fqn, package, relative_uri

## Résolution de problèmes

### Service ASM ne démarre pas
1. Java : `java -version` (Java 11+ requis)
2. Build : `cd Extracteurs/ASMAnalysisService && gradlew.bat build`
3. Run : `gradlew.bat service:run`

### Base vide ou pas de résultats
1. Vérifier : `ls .callgraph.db`
2. Stats : Utiliser l'outil `read_query` de votre serveur SQLite MCP avec `"SELECT COUNT(*) FROM nodes"`
3. Ré-extraire : `python run_asm_extraction.py . --init` (reset complet)

### Gradle errors
1. Vérifier `gradlew.bat` existe
2. Test : `./gradlew.bat --version`

## Conventions de nommage

**PascalCase** pour fichiers Python :
- `ASMExtractor.py` → `class ASMExtractor`
- `GradleDependencyManager.py` → `class GradleDependencyManager`

## Design Patterns

**3-step extraction (toujours exécutée)** :
1. Package Discovery (Gradle)
2. Symbol Indexing (FQN → URI) + auto-clean des packages modifiés
3. Call Graph Extraction (Nodes + Edges) avec métadonnées enrichies

**Modes d'extraction** :
- **Incrémental (défaut)** : `ASMExtractor(init=False)` + `clean_package_data()` automatique
- **INIT (--init)** : `ASMExtractor(init=True)` → `init_database()` appelé automatiquement

**Méthodes ASMExtractor** :
- `__init__(init=False)` : Constructeur avec mode incrémental par défaut
- `__init__(init=True)` : Constructeur avec full reset (drop all tables)
- `init_database()` : Drop et recrée toutes les tables (appelé automatiquement si init=True)
- `clean_package_data(package)` : Supprime données d'un package spécifique (incremental mode)
- `_extract_visibility(modifiers)` : Extrait visibilité depuis modifiers ASM

## Fichiers de configuration

### mcp.json (MCP servers)
Exemple de configuration pour un serveur SQLite MCP :
```json
{
  "mcpServers": {
    "<your-server-name>": {
      "command": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "C:/path/to/project/.callgraph.db"]
    }
  }
}
```

**Note** : Remplacer `<your-server-name>` par le nom que vous souhaitez donner au serveur.

## Ressources

- [README.md](README.md) - Quick start et overview
- [ASM_ARCHITECTURE.md](ASM_ARCHITECTURE.md) - Architecture complète et détaillée
- [ASM Documentation](https://asm.ow2.io/)
- [SQLite](https://www.sqlite.org/)