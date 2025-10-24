# TypeScriptASTService

Service HTTP d'analyse sémantique de code TypeScript/React utilisant le compilateur TypeScript.

## Description

TypeScriptASTService est un service HTTP qui analyse des fichiers TypeScript/React pour extraire les relations d'usage entre symboles (fonctions, composants React, hooks, imports, etc.). Il utilise le **compilateur TypeScript** pour résoudre les symboles et identifier leurs emplacements de définition.

### Caractéristiques principales

- Analyse AST complète avec résolution de types TypeScript
- Support spécifique pour React (composants JSX, hooks)
- Extraction des usages bidirectionnels (caller → callee et callee → caller)
- Résolution des emplacements de définition (fichier source + numéro de ligne)
- Format de sortie standardisé cross-langage (compatible avec JavaASTService)
- Détection des imports et résolution de modules
- API HTTP RESTful

## Prérequis

- Node.js 16+
- npm ou yarn

## Installation

```bash
cd scripts/TypeScriptASTService
npm install
```

## Démarrage

```bash
# Mode développement (avec auto-reload)
npm run dev

# Mode production
npm start

# Build
npm run build
```

Le service démarre sur le port **8766** par défaut.

## API

### POST /analyze

Analyse un fichier TypeScript/React et extrait ses usages.

**Requête:**
```json
{
  "filePath": "C:/path/to/component.tsx",
  "fileContent": "import React from 'react';\n...",
  "projectRoot": "C:/path/to/project"
}
```

**Réponse:**
```json
{
  "success": true,
  "filePath": "C:/path/to/component.tsx",
  "usages": [
    {
      "usageType": "import",
      "callerUri": "file:///C:/path/to/component.tsx",
      "callerLine": 1,
      "callerSymbol": "MyComponent",
      "callerKind": "react_component",
      "calleeUri": "file:///C:/path/to/node_modules/react/index.d.ts",
      "calleeSymbol": "React",
      "calleeLine": 42,
      "calleeKind": "module",
      "stringContext": "react"
    },
    {
      "usageType": "react_component",
      "callerUri": "file:///C:/path/to/component.tsx",
      "callerLine": 15,
      "callerSymbol": "MyComponent",
      "callerKind": "react_component",
      "calleeUri": "file:///C:/path/to/Button.tsx",
      "calleeSymbol": "Button",
      "calleeLine": 8,
      "calleeKind": "react_component",
      "stringContext": null
    }
  ]
}
```

### GET /health

Vérifie que le service est en ligne.

**Réponse:**
```json
{
  "status": "ok",
  "service": "TypeScriptASTService"
}
```

### POST /shutdown

Arrête le service proprement.

**Réponse:**
```json
{
  "status": "shutting down"
}
```

## Format de sortie standardisé

Chaque usage suit le format standardisé cross-langage:

| Champ | Type | Description |
|-------|------|-------------|
| `usageType` | string | Type d'usage (voir types ci-dessous) |
| `callerUri` | string | URI du fichier appelant (format file:///) |
| `callerLine` | number | Ligne où l'usage se produit |
| `callerSymbol` | string \| null | Nom du symbole appelant |
| `callerKind` | string \| null | Type du symbole appelant |
| `calleeUri` | string \| null | URI du fichier du symbole appelé (résolu si possible) |
| `calleeSymbol` | string \| null | Nom du symbole appelé |
| `calleeLine` | number \| null | Ligne de définition du symbole appelé (résolu si possible) |
| `calleeKind` | string \| null | Type du symbole appelé |
| `stringContext` | string \| null | Contexte additionnel (ex: module path pour imports) |

### Types d'usage

**Imports/Exports:**
- `import` - Import de symbole
- `import_used_by` - Relation inverse (import utilisé par)

**React:**
- `react_component` - Utilisation de composant React dans JSX
- `react_component_used_by` - Relation inverse (composant utilisé par)
- `hook_call` - Appel de hook React (useState, useEffect, etc.)
- `hook_called_by` - Relation inverse (hook appelé par)

**TypeScript:**
- `function_call` - Appel de fonction
- `function_called_by` - Relation inverse (fonction appelée par)
- `type_reference` - Référence à un type TypeScript
- `type_referenced_by` - Relation inverse (type référencé par)
- `interface_implementation` - Implémentation d'interface
- `class_extends` - Héritage de classe

### Résolution des symboles

Le service utilise le compilateur TypeScript pour résoudre les symboles:

**Résolution réussie:**
```json
{
  "calleeUri": "file:///C:/Users/nicolasv/axelor-ui/src/components/Button.tsx",
  "calleeLine": 12
}
```

**Résolution échouée** (symbole externe ou non trouvé):
```json
{
  "calleeUri": null,
  "calleeLine": null
}
```

## Architecture

### server.ts
- Point d'entrée principal
- Configuration du serveur Express
- Endpoints HTTP
- Gestion des erreurs

### Analyse TypeScript

Le service crée un **Program TypeScript** pour chaque analyse:

```typescript
const program = ts.createProgram([filePath], {
  target: ts.ScriptTarget.ESNext,
  module: ts.ModuleKind.ESNext,
  jsx: ts.JsxEmit.React,
  moduleResolution: ts.ModuleResolutionKind.NodeJs,
  esModuleInterop: true,
  allowSyntheticDefaultImports: true
}, host);

const checker = program.getTypeChecker();
```

Le **TypeChecker** permet de:
- Résoudre les symboles importés
- Trouver les déclarations de symboles
- Obtenir les types inférés
- Analyser les relations entre symboles

### Résolution de symboles

Pour résoudre un symbole utilisé:

```typescript
function resolveSymbol(node: ts.Node, name: string) {
  const symbol = checker.getSymbolAtLocation(node);
  if (!symbol) return null;

  const declarations = symbol.getDeclarations();
  if (!declarations || declarations.length === 0) return null;

  const decl = declarations[0];
  const sourceFile = decl.getSourceFile();
  const { line } = sourceFile.getLineAndCharacterOfPosition(decl.getStart());

  return {
    uri: pathToFileUri(sourceFile.fileName),
    lineNumber: line + 1,
    kind: getSymbolKind(symbol)
  };
}
```

### Détection de composants React

Le service détecte les composants React dans JSX:

```typescript
if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
  const tagName = node.tagName;

  // Ignore les éléments HTML natifs
  if (ts.isIdentifier(tagName)) {
    const name = tagName.text;
    if (name[0] === name[0].toUpperCase()) {
      // C'est un composant React (commence par majuscule)
      const resolved = resolveSymbol(tagName, name);
      // Créer usage + bidirectionnel
    }
  }
}
```

### Détection de hooks React

Le service détecte les hooks React (fonctions commençant par `use`):

```typescript
if (ts.isCallExpression(node)) {
  const expr = node.expression;
  if (ts.isIdentifier(expr)) {
    const name = expr.text;
    if (name.startsWith('use')) {
      // C'est probablement un hook React
      const resolved = resolveSymbol(expr, name);
      // Créer usage de type hook_call + bidirectionnel
    }
  }
}
```

## Configuration TypeScript

Le service utilise la configuration suivante pour l'analyse:

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "commonjs",
    "lib": ["ES2020"],
    "jsx": "react",
    "moduleResolution": "node",
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "skipLibCheck": true,
    "strict": true
  }
}
```

## Exemple d'utilisation

```python
import requests
from pathlib import Path

# Lire le fichier à analyser
file_path = Path("C:/Users/nicolasv/axelor-ui/src/components/Button.tsx")
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Analyser le fichier
response = requests.post(
    'http://localhost:8766/analyze',
    json={
        'filePath': str(file_path),
        'fileContent': content,
        'projectRoot': str(file_path.parent.parent)
    }
)

result = response.json()
print(f"Success: {result['success']}")
print(f"Usages found: {len(result['usages'])}")

# Afficher les imports
imports = [u for u in result['usages'] if u['usageType'] == 'import']
print(f"\nImports ({len(imports)}):")
for imp in imports:
    print(f"  {imp['calleeSymbol']} from {imp['stringContext']}")

# Afficher les composants React utilisés
components = [u for u in result['usages'] if u['usageType'] == 'react_component']
print(f"\nReact Components ({len(components)}):")
for comp in components:
    print(f"  <{comp['calleeSymbol']}> at line {comp['callerLine']}")
    if comp['calleeUri']:
        print(f"    Defined in: {comp['calleeUri']}:{comp['calleeLine']}")
```

## Scripts disponibles

```bash
# Démarrer en mode développement
npm run dev

# Build pour production
npm run build

# Démarrer en production
npm start

# Lancer les tests
npm test
```

## Limitations connues

- La résolution nécessite un projet TypeScript valide avec tsconfig.json
- Les symboles externes (node_modules) peuvent ne pas être résolus si les types ne sont pas disponibles
- Les composants dynamiques (ex: `components[name]`) ne sont pas détectés
- Les hooks personnalisés doivent commencer par `use` pour être détectés

## Dépendances principales

- TypeScript 5.x (compilateur et type checker)
- Express (serveur HTTP)
- ts-node (exécution TypeScript)
- @types/node (types Node.js)

## Différences avec JavaASTService

| Aspect | JavaASTService | TypeScriptASTService |
|--------|----------------|----------------------|
| Port | 8765 | 8766 |
| Input | Liste de fichiers | 1 fichier + contenu |
| Résolution | JavaSymbolSolver | TypeScript Compiler API |
| Formats | .java | .ts, .tsx, .js, .jsx |
| Spécificités | Annotations, interfaces Java | React, hooks, JSX |
