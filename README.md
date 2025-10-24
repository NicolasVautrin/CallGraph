# CallGraph MCP Server

Serveur MCP (Model Context Protocol) pour l'analyse du call graph de projets Axelor.

## Description

Le serveur MCP CallGraph permet à Claude Code d'interroger intelligemment le call graph d'un projet Axelor :
- Trouver où une méthode/classe est utilisée
- Analyser l'impact de modifications
- Naviguer dans les dépendances
- Tracer les chaînes d'appels

Le serveur s'intègre directement dans Claude Desktop et détecte automatiquement la base de données du projet courant.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                  Claude Desktop                          │
│  - Interface utilisateur                                 │
│  - Envoie requêtes MCP                                   │
└────────────────────┬─────────────────────────────────────┘
                     │ MCP Protocol (stdio)
                     ▼
┌──────────────────────────────────────────────────────────┐
│           MCP CallGraph Server                           │
│  (mcp_callgraph_server.py)                               │
│  - Initialisation au démarrage de Claude Desktop         │
│  - Auto-détecte la DB du projet via Path.cwd()          │
│  - Expose 8 outils MCP                                   │
└────────────────────┬─────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────┐
│           CallGraphService                               │
│  (call_graph_service.py)                                 │
│  - Requêtes ChromaDB                                     │
│  - Formatage résultats                                   │
│  - Récursion et filtrage                                 │
└────────────────────┬─────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────┐
│              ChromaDB Vector Database                    │
│  (.vector-raw-db ou .vector-semantic-db)                 │
│  - Stockage des usages Java/XML                          │
│  - Requêtes par métadonnées                              │
│  - Recherche sémantique (si embeddings)                  │
└──────────────────────────────────────────────────────────┘
```

## Installation

### 1. Configuration Claude Desktop

Éditer `claude_desktop_config.json` :

**Windows** : `%APPDATA%\Claude\claude_desktop_config.json`

**macOS** : `~/Library/Application Support/Claude/claude_desktop_config.json`

**Linux** : `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "callgraph": {
      "command": "python",
      "args": [
        "C:/Users/nicolasv/MCP_servers/CallGraph/mcp_callgraph_server.py"
      ]
    }
  }
}
```

### 2. Vérification des dépendances

Le serveur nécessite :
- Python 3.8+
- ChromaDB
- MCP SDK

```bash
cd C:\Users\nicolasv\MCP_servers\CallGraph
pip install -r requirements.txt
```

### 3. Redémarrer Claude Desktop

Le serveur démarre automatiquement avec Claude Desktop.

**Vérification** : Dans Claude Code, les outils MCP `mcp__callgraph__*` doivent être disponibles.

## Fonctionnement

### Auto-détection de la base de données

Le serveur détecte automatiquement la DB du projet courant :

```python
def init_service():
    cwd = Path.cwd()  # Répertoire du projet courant

    # Priorité : semantic > raw
    semantic_db = cwd / ".vector-semantic-db"
    raw_db = cwd / ".vector-raw-db"

    if semantic_db.exists():
        db_path = semantic_db
        logger.info("Using semantic database (with embeddings)")
    elif raw_db.exists():
        db_path = raw_db
        logger.info("Using raw database (metadata only)")
    else:
        db_path = raw_db  # Will be created on extract
        logger.info("No database found, will use raw DB when created")

    service = CallGraphService(str(db_path))
```

**Comportement** :
- Claude Code ouvert sur `C:\Bricklead_Encheres\` → Utilise `C:\Bricklead_Encheres\.vector-raw-db\`
- Claude Code ouvert sur `C:\other-project\` → Utilise `C:\other-project\.vector-raw-db\`
- Pas de redémarrage nécessaire : le cwd change avec le projet

### Workflow typique

```
1. Utilisateur : "Où est utilisée la méthode validateMove ?"

2. Claude Code :
   - Appelle mcp__callgraph__find_usages
   - symbol="validateMove"
   - depth=0 (pas de récursion)

3. MCP Server :
   - Lit la DB du projet courant
   - Requête ChromaDB avec filtre sur symbol
   - Formate les résultats en arbre

4. Claude Code :
   - Reçoit les résultats formatés
   - Répond à l'utilisateur avec contexte
```

## Outils MCP disponibles

### 1. find_usages

Trouve tous les usages d'un symbole avec récursion optionnelle.

**Paramètres** :
- `symbol` (required) : Nom du symbole à chercher
- `usage_type` (optional) : Type d'usage à filtrer
- `module_filter` (optional) : Filtrer par module
- `exclude_generated` (default: true) : Exclure fichiers générés
- `offset` (default: 0) : Pagination
- `limit` (default: 20) : Nombre max de résultats
- `depth` (default: 0) : Profondeur récursion (0=aucune, -1=max, N=N niveaux)
- `max_children_per_level` (default: 10) : Max enfants par niveau

**Exemple** :
```python
# Trouver les usages directs
find_usages(symbol="validateMove", depth=0)

# Trouver qui appelle, et qui appelle les appelants (2 niveaux)
find_usages(symbol="validateMove", depth=2)

# Récursion complète dans un module
find_usages(
    symbol="computeTotal",
    depth=-1,
    module_filter="open-auction-base"
)
```

**Retour** :
```
Direct usages of 'validateMove' (5 results, showing 1-5):

[java_method_call] Move.validateMove() called by:
  File: MoveController.java:156
  Caller: com.axelor.apps.controller.MoveController.processMove
  Module: open-auction-base

[java_method_call] Move.validateMove() called by:
  File: MoveService.java:89
  Caller: com.axelor.apps.service.MoveService.validate
  Module: open-auction-base

Total: 5 usages
```

### 2. get_definition

Trouve où un symbole est défini.

**Paramètres** :
- `symbol` (required) : Nom du symbole

**Exemple** :
```python
get_definition(symbol="MoveValidateService")
```

**Retour** :
```json
{
  "definitions": [
    {
      "symbol": "MoveValidateService",
      "source_file": "modules/open-auction-base/src/.../MoveValidateService.java",
      "line": 42,
      "module": "open-auction-base",
      "usage_type": "java_declaration"
    }
  ],
  "total": 1
}
```

### 3. find_callers

Trouve qui appelle une méthode (version simplifiée de find_usages).

**Paramètres** :
- `symbol` (required) : Nom de la méthode
- `offset` (default: 0) : Pagination
- `limit` (default: 20) : Nombre max de résultats

**Exemple** :
```python
find_callers(symbol="validateMove")
```

### 4. find_callees

Trouve ce qu'appelle une méthode.

**Paramètres** :
- `symbol` (required) : Nom de la méthode
- `offset` (default: 0) : Pagination
- `limit` (default: 20) : Nombre max de résultats

**Exemple** :
```python
find_callees(symbol="processMove")
```

**Retour** :
```json
{
  "callees": [
    {
      "callee_symbol": "validateMove",
      "callee_fqn": "com.axelor.apps.base.Move.validateMove",
      "source_file": "MoveController.java",
      "line": 156
    },
    {
      "callee_symbol": "computeTotal",
      "callee_fqn": "com.axelor.apps.base.Move.computeTotal",
      "source_file": "MoveController.java",
      "line": 157
    }
  ],
  "total": 2
}
```

### 5. impact_analysis

Analyse d'impact récursive : qui appelle ce symbole, et qui appelle ces appelants (effet cascade).

**Paramètres** :
- `symbol` (required) : Symbole de départ
- `depth` (default: 2) : Profondeur (max recommandé: 5)
- `only_custom` (default: false) : Exclure framework Axelor
- `offset` (default: 0) : Pagination root level
- `limit` (default: 50) : Max résultats par niveau

**Exemple** :
```python
impact_analysis(
    symbol="validateMove",
    depth=3,
    only_custom=True
)
```

**Retour** :
```
Impact Analysis for 'validateMove' (depth=3)

Level 0: validateMove
└─ 5 direct callers

Level 1: Who calls the callers
├─ processMove (MoveController.java:156)
│  └─ 3 callers
├─ validate (MoveService.java:89)
│  └─ 12 callers
└─ ...

Level 2: Who calls Level 1
├─ handleRequest (RestController.java:45)
│  └─ 8 callers
└─ ...

Total impact: 28 methods affected
```

### 6. search_by_file

Trouve tous les usages dans un fichier spécifique.

**Paramètres** :
- `file_path` (required) : Chemin du fichier (partiel ou complet)
- `offset` (default: 0) : Pagination
- `limit` (default: 50) : Nombre max de résultats

**Exemple** :
```python
search_by_file(file_path="MoveValidateService.java")
```

**Retour** :
```json
{
  "usages": [
    {
      "line": 42,
      "usage_type": "java_method_call",
      "callee_symbol": "computeTotal",
      "caller_symbol": "validate"
    },
    {
      "line": 45,
      "usage_type": "java_method_call",
      "callee_symbol": "persist",
      "caller_symbol": "validate"
    }
  ],
  "total": 15,
  "file": "modules/.../MoveValidateService.java"
}
```

### 7. get_stats

Statistiques de la base de données.

**Paramètres** :
- `module` (optional) : Filtrer par module

**Exemple** :
```python
# Stats globales
get_stats()

# Stats d'un module
get_stats(module="open-auction-base")
```

**Retour** :
```json
{
  "total_usages": 456789,
  "by_type": {
    "java_method_call": 123456,
    "java_declaration": 98765,
    "java_constructor_call": 23456,
    "java_field_access": 12345,
    "xml_action_method": 3456
  },
  "by_module": {
    "open-auction-base": 123456,
    "open-auction-vehicule": 87654,
    "vpauto": 45678
  },
  "sources": {
    "java": 400000,
    "xml": 56789
  }
}
```

### 8. extract

Lance l'extraction du call graph.

**Paramètres** :
- `mode` (required) : "full" ou "local"
- `reset` (default: true pour full, false pour local) : Reset DB avant extraction

**Exemple** :
```python
# Extraction complète (Axelor + local)
extract(mode="full", reset=True)

# Extraction locale uniquement
extract(mode="local")
```

**Retour** :
```json
{
  "status": "success",
  "mode": "full",
  "reset": true,
  "total_entries": 456789,
  "log": "... logs d'extraction ..."
}
```

## Cas d'usage

### 1. Comprendre l'impact d'un changement

**Question** : "Si je modifie la méthode `validateMove`, qu'est-ce qui sera impacté ?"

**Commande** :
```python
impact_analysis(symbol="validateMove", depth=3, only_custom=True)
```

**Résultat** :
- Niveau 1 : 5 méthodes appellent directement `validateMove`
- Niveau 2 : 18 méthodes appellent ces 5 méthodes
- Niveau 3 : 47 méthodes appellent les 18
- **Total : ~70 méthodes potentiellement impactées**

### 2. Tracer un bug

**Question** : "D'où vient cet appel à `computeTotal` qui plante ?"

**Commande 1** : Trouver tous les appelants
```python
find_callers(symbol="computeTotal")
```

**Commande 2** : Pour chaque appelant suspect, voir ce qu'il appelle
```python
find_callees(symbol="MoveController.processMove")
```

**Résultat** : Chaîne d'appel identifiée

### 3. Naviguer dans le code

**Question** : "Où est défini `MoveValidateService` ?"

**Commande** :
```python
get_definition(symbol="MoveValidateService")
```

**Résultat** :
```
File: modules/open-auction-base/src/.../MoveValidateService.java:42
```

### 4. Refactoring

**Question** : "Cette méthode `oldMethod` est-elle encore utilisée ?"

**Commande** :
```python
find_usages(symbol="oldMethod", depth=0)
```

**Résultat** :
- Si 0 usages → Safe to delete
- Si N usages → Identifier et migrer

### 5. Analyse de module

**Question** : "Quelles sont les dépendances entre modules ?"

**Commande 1** : Stats du module
```python
get_stats(module="open-auction-vehicule")
```

**Commande 2** : Usages inter-modules
```python
find_usages(
    symbol="Vehicle",
    module_filter="!open-auction-vehicule"  # Hors du module
)
```

## Configuration avancée

### Variables d'environnement

Le serveur peut être configuré via environnement :

```bash
# Forcer un chemin de DB
export CALLGRAPH_DB_PATH="/path/to/.vector-raw-db"

# Logger level
export CALLGRAPH_LOG_LEVEL="DEBUG"
```

### Extraction depuis le serveur

L'outil `extract` permet de lancer l'extraction sans quitter Claude Code :

```python
# Premier projet : Extraction complète
extract(mode="full")
# → Crée .vector-raw-db dans le projet courant

# Modifications locales : Re-extraction rapide
extract(mode="local")
# → Met à jour uniquement les modules/
```

## Dépendances du projet

### Structure minimale attendue

Le serveur fonctionne sur tout projet avec :

```
project/
├─ .vector-raw-db/          # DB créée par extraction
│  └─ chroma.sqlite3
└─ modules/                 # Code du projet (optionnel pour queries)
   └─ ...
```

### Projet Axelor typique

```
project/
├─ .vector-raw-db/          # DB du call graph
├─ modules/                 # Modules custom
│  ├─ open-auction-base/
│  ├─ open-auction-vehicule/
│  └─ ...
├─ gradle.properties        # Versions Axelor (pour extraction)
└─ build.gradle            # Config projet
```

## Logs et debugging

### Activer les logs

Les logs du serveur sont visibles dans les logs Claude Desktop :

**Windows** : `%APPDATA%\Claude\logs\mcp*.log`

**macOS** : `~/Library/Logs/Claude/mcp*.log`

**Exemple** :
```
INFO - Call graph service initialized with db: C:\Bricklead_Encheres\.vector-raw-db
INFO - Using raw database (metadata only)
```

### Tester le serveur manuellement

```python
# Test direct (hors MCP)
import sys
from pathlib import Path
sys.path.insert(0, str(Path("Extracteurs")))

from call_graph_service import CallGraphService

service = CallGraphService(".vector-raw-db")
result = service.find_usages("validateMove", depth=0)
print(service.format_result("find_usages", result, symbol="validateMove"))
```

### Problèmes courants

**1. "No database found"**

**Cause** : Aucune DB dans le projet courant

**Solution** :
```python
extract(mode="full")
```

**2. "Service initialization failed"**

**Cause** : Dépendances manquantes

**Solution** :
```bash
pip install chromadb mcp
```

**3. "Empty results"**

**Cause** : DB vide ou symbole introuvable

**Solution** :
- Vérifier stats : `get_stats()`
- Réextraire : `extract(mode="full", reset=True)`

## Performance

### Requêtes

- **find_usages** (depth=0) : ~10-50ms
- **find_usages** (depth=2) : ~100-500ms
- **impact_analysis** (depth=3) : ~200ms-1s
- **get_stats** : ~50ms

### Taille de DB

- **Petit projet** (~500 fichiers) : ~50 MB
- **Projet moyen** (~2000 fichiers) : ~150 MB
- **Gros projet** (~7000 fichiers) : ~250 MB

### Mémoire

- MCP Server : ~50-100 MB
- ChromaDB : ~200-500 MB selon taille DB

## Développement

### Ajouter un nouvel outil MCP

1. **Définir l'outil** dans `list_tools()` :
```python
Tool(
    name="my_new_tool",
    description="...",
    inputSchema={...}
)
```

2. **Implémenter le handler** dans `call_tool()` :
```python
if name == "my_new_tool":
    result = service.my_method(arguments["param"])
    return [TextContent(type="text", text=json.dumps(result))]
```

3. **Ajouter la méthode** dans `call_graph_service.py` :
```python
def my_method(self, param):
    # Requête ChromaDB
    # Formatage
    return result
```

### Tests

```bash
# Test du service
cd Extracteurs
python -m pytest test_call_graph_service.py

# Test extraction
python extraction_manager.py --project-root "." --local
```

## Limitations

- **Pas de hot-reload** : Changement de DB nécessite relance Claude Desktop
- **Un projet à la fois** : Le serveur détecte le projet via cwd
- **Pas de merge DB** : Impossible de requêter plusieurs projets simultanément
- **Mémoire** : Garder ChromaDB en mémoire (~200-500 MB)

## Roadmap

- [ ] Support multi-projets (plusieurs DB en parallèle)
- [ ] Hot-reload de DB (détection changements)
- [ ] Recherche sémantique (si .vector-semantic-db)
- [ ] Export résultats (JSON, CSV)
- [ ] Graphes visuels (Graphviz, D3.js)
- [ ] Analyse de complexité cyclomatique
- [ ] Détection de code mort

## Ressources

- **Extracteurs** : Voir `Extracteurs/README.md`
- **JavaASTService** : Voir `Extracteurs/JavaASTService/README.md`
- **MCP Protocol** : https://modelcontextprotocol.io
- **ChromaDB** : https://www.trychroma.com
