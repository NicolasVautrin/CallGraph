import express, { Request, Response } from 'express';
import * as ts from 'typescript';
import * as path from 'path';
import * as fs from 'fs';

const app = express();
const PORT = 8766;

app.use(express.json({ limit: '50mb' }));

interface TsUsage {
  usageType: string;
  callerUri: string;                // File URI where the usage occurs (file:///...)
  callerLine: number;
  callerSymbol: string | null;      // Symbol name of the caller (function/method/component making the call)
  callerKind: string | null;        // Type of caller (function, method, component, etc.)
  calleeUri: string | null;         // File URI where symbol is defined (file:///...)
  calleeSymbol: string | null;      // Symbol name in native notation
  calleeLine: number | null;        // Line number in definition file
  calleeKind: string | null;        // function, class, hook, component, etc.
  stringContext: string | null;
}

/**
 * TypeScript AST Analyzer with semantic resolution using TypeScript Compiler API
 */
class TypeScriptAnalyzer {
  private program: ts.Program | null = null;
  private checker: ts.TypeChecker | null = null;
  private sourceFile: ts.SourceFile | null = null;

  /**
   * Analyze a TypeScript file and extract usages with semantic resolution
   */
  analyze(filePath: string, fileContent: string, projectRoot: string): TsUsage[] {
    const usages: TsUsage[] = [];

    // Create a compiler host
    const compilerOptions: ts.CompilerOptions = {
      target: ts.ScriptTarget.Latest,
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.React,
      esModuleInterop: true,
      moduleResolution: ts.ModuleResolutionKind.NodeJs,
      baseUrl: projectRoot,
    };

    // Create in-memory host
    const host = ts.createCompilerHost(compilerOptions);
    const originalGetSourceFile = host.getSourceFile;

    host.getSourceFile = (fileName, languageVersion, onError, shouldCreateNewSourceFile) => {
      if (fileName === filePath) {
        return ts.createSourceFile(fileName, fileContent, languageVersion, true, ts.ScriptKind.TSX);
      }
      return originalGetSourceFile(fileName, languageVersion, onError, shouldCreateNewSourceFile);
    };

    // Create program
    this.program = ts.createProgram([filePath], compilerOptions, host);
    this.checker = this.program.getTypeChecker();
    this.sourceFile = this.program.getSourceFile(filePath)!;

    if (!this.sourceFile) {
      throw new Error(`Could not create source file for ${filePath}`);
    }

    // Visit all nodes
    this.visitNode(this.sourceFile, usages);

    return usages;
  }

  private visitNode(node: ts.Node, usages: TsUsage[]): void {
    // Handle imports
    if (ts.isImportDeclaration(node)) {
      this.handleImport(node, usages);
    }

    // Handle function calls
    if (ts.isCallExpression(node)) {
      this.handleCallExpression(node, usages);
    }

    // Handle JSX elements (React components)
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      this.handleJsxElement(node, usages);
    }

    // Handle exports
    if (ts.isExportDeclaration(node) || ts.isExportAssignment(node)) {
      this.handleExport(node, usages);
    }

    // Recurse into children
    ts.forEachChild(node, child => this.visitNode(child, usages));
  }

  /**
   * Convert file path to URI format (file:///) with optional line number
   */
  private pathToUri(filePath: string, line?: number | null): string {
    // Normalize path separators to forward slashes
    const normalized = filePath.replace(/\\/g, '/');

    let uri: string;
    // Add file:// prefix if not already present
    if (normalized.startsWith('file:///')) {
      uri = normalized;
    } else if (normalized.match(/^[a-zA-Z]:\//)) {
      // Windows absolute path: C:/... -> file:///C:/...
      uri = `file:///${normalized}`;
    } else if (normalized.startsWith('/')) {
      // Unix absolute path: /... -> file:///...
      uri = `file://${normalized}`;
    } else {
      // Relative paths (shouldn't happen, but handle gracefully)
      uri = `file:///${normalized}`;
    }

    // Append line number if provided
    if (line != null && line > 0) {
      uri = `${uri}:${line}`;
    }

    return uri;
  }

  /**
   * Determine the kind of symbol from its declaration
   */
  private getSymbolKind(declaration: ts.Node): string {
    if (ts.isFunctionDeclaration(declaration)) return 'function';
    if (ts.isMethodDeclaration(declaration)) return 'method';
    if (ts.isClassDeclaration(declaration)) return 'class';
    if (ts.isInterfaceDeclaration(declaration)) return 'interface';
    if (ts.isTypeAliasDeclaration(declaration)) return 'type';
    if (ts.isEnumDeclaration(declaration)) return 'enum';
    if (ts.isModuleDeclaration(declaration)) return 'namespace';

    // Variable declarations (could be functions, components, constants)
    if (ts.isVariableDeclaration(declaration)) {
      const init = declaration.initializer;
      if (init) {
        if (ts.isArrowFunction(init) || ts.isFunctionExpression(init)) {
          // Check if it looks like a React component (starts with uppercase)
          const name = declaration.name.getText();
          if (name && /^[A-Z]/.test(name)) {
            return 'component';
          }
          return 'function';
        }
      }
      return 'const';
    }

    // Arrow functions and function expressions
    if (ts.isArrowFunction(declaration) || ts.isFunctionExpression(declaration)) {
      return 'function';
    }

    return 'unknown';
  }

  private handleImport(node: ts.ImportDeclaration, usages: TsUsage[]): void {
    if (!this.sourceFile || !this.checker) return;

    const moduleSpecifier = node.moduleSpecifier;
    if (!ts.isStringLiteral(moduleSpecifier)) return;

    const importPath = moduleSpecifier.text;

    // Skip external modules (node_modules)
    if (!importPath.startsWith('.') && !importPath.startsWith('/')) {
      return;
    }

    const importClause = node.importClause;
    if (!importClause) return;

    // Helper to resolve symbol to its actual declaration file, line number, and kind
    const resolveSymbol = (identifier: ts.Identifier): {
      path: string | null;
      line: number | null;
      kind: string | null;
      symbolName: string;
    } => {
      const symbol = this.checker!.getSymbolAtLocation(identifier);
      const symbolName = identifier.text;

      if (!symbol) {
        return {
          path: this.resolveModuleName(importPath),
          line: null,
          kind: null,
          symbolName
        };
      }

      // Follow aliases (re-exports)
      const aliasedSymbol = this.checker!.getAliasedSymbol(symbol);
      const targetSymbol = aliasedSymbol !== symbol ? aliasedSymbol : symbol;

      // Get declarations
      const declarations = targetSymbol.getDeclarations();
      if (declarations && declarations.length > 0) {
        const declaration = declarations[0];
        const sourceFile = declaration.getSourceFile();
        if (sourceFile) {
          const line = ts.getLineAndCharacterOfPosition(sourceFile, declaration.getStart()).line + 1;
          const kind = this.getSymbolKind(declaration);
          return {
            path: sourceFile.fileName,
            line,
            kind,
            symbolName
          };
        }
      }

      return {
        path: this.resolveModuleName(importPath),
        line: null,
        kind: null,
        symbolName
      };
    };

    // Default import
    if (importClause.name) {
      const resolved = resolveSymbol(importClause.name);
      // Skip if resolved to node_modules
      if (resolved.path && resolved.path.includes('node_modules')) return;

      const callerLine = ts.getLineAndCharacterOfPosition(this.sourceFile, node.getStart()).line + 1;
      usages.push({
        usageType: 'import',
        callerUri: this.pathToUri(this.sourceFile.fileName, callerLine),
        callerLine,
        callerSymbol: null,  // Imports are at module level
        callerKind: 'module',
        calleeUri: resolved.path ? this.pathToUri(resolved.path, resolved.line) : null,
        calleeSymbol: resolved.symbolName,
        calleeLine: resolved.line,
        calleeKind: resolved.kind,
        stringContext: `from:${importPath}`,
      });
    }

    // Named imports
    if (importClause.namedBindings) {
      if (ts.isNamedImports(importClause.namedBindings)) {
        importClause.namedBindings.elements.forEach(element => {
          const resolved = resolveSymbol(element.name);
          // Skip if resolved to node_modules
          if (resolved.path && resolved.path.includes('node_modules')) return;

          const callerLine = ts.getLineAndCharacterOfPosition(this.sourceFile!, node.getStart()).line + 1;
          usages.push({
            usageType: 'import',
            callerUri: this.pathToUri(this.sourceFile!.fileName, callerLine),
            callerLine,
            callerSymbol: null,  // Imports are at module level
            callerKind: 'module',
            calleeUri: resolved.path ? this.pathToUri(resolved.path, resolved.line) : null,
            calleeSymbol: resolved.symbolName,
            calleeLine: resolved.line,
            calleeKind: resolved.kind,
            stringContext: `from:${importPath}`,
          });
        });
      }
    }
  }

  private handleCallExpression(node: ts.CallExpression, usages: TsUsage[]): void {
    if (!this.sourceFile || !this.checker) return;

    const expression = node.expression;
    let functionName: string | null = null;
    let usageType = 'function_call';

    // Get function name
    if (ts.isIdentifier(expression)) {
      functionName = expression.text;

      // Check if it's a React hook
      if (functionName.startsWith('use')) {
        usageType = 'hook_call';
      }
    } else if (ts.isPropertyAccessExpression(expression)) {
      functionName = expression.name.text;
    }

    if (!functionName) return;

    // Try to resolve the definition
    const symbol = this.checker.getSymbolAtLocation(expression);
    let resolvedPath: string | null = null;
    let calleeLine: number | null = null;
    let calleeKind: string | null = null;

    if (symbol) {
      // Follow aliases (imported symbols) only if it's actually an alias
      let targetSymbol = symbol;
      if ((symbol.flags & ts.SymbolFlags.Alias) !== 0) {
        targetSymbol = this.checker.getAliasedSymbol(symbol);
      }

      const declarations = targetSymbol.getDeclarations();
      if (declarations && declarations.length > 0) {
        const declaration = declarations[0];
        const sourceFile = declaration.getSourceFile();
        if (sourceFile) {
          resolvedPath = sourceFile.fileName;
          // Extract line number of the definition
          calleeLine = ts.getLineAndCharacterOfPosition(sourceFile, declaration.getStart()).line + 1;
          calleeKind = this.getSymbolKind(declaration);
        }
      }
    }

    // Override kind for hooks
    if (usageType === 'hook_call') {
      calleeKind = 'hook';
    }

    // Skip if resolved to node_modules
    if (resolvedPath && resolvedPath.includes('node_modules')) return;

    const callerContext = this.getCallerContext(node);
    const callerLine = ts.getLineAndCharacterOfPosition(this.sourceFile, node.getStart()).line + 1;
    const usage: TsUsage = {
      usageType,
      callerUri: this.pathToUri(this.sourceFile.fileName, callerLine),
      callerLine,
      callerSymbol: callerContext.symbol,
      callerKind: callerContext.kind,
      calleeUri: resolvedPath ? this.pathToUri(resolvedPath, calleeLine) : null,
      calleeSymbol: functionName,
      calleeLine,
      calleeKind,
      stringContext: null,
    };

    usages.push(usage);

    // Create bidirectional reference (callee is called by caller)
    if (usage.calleeUri && usage.calleeLine) {
      usages.push(this.createBidirectionalUsage(usage));
    }
  }

  private handleJsxElement(node: ts.JsxOpeningElement | ts.JsxSelfClosingElement, usages: TsUsage[]): void {
    if (!this.sourceFile || !this.checker) return;

    const tagName = node.tagName;
    if (!ts.isIdentifier(tagName)) return;

    const componentName = tagName.text;

    // Only track components (start with uppercase)
    if (!componentName || !/^[A-Z]/.test(componentName)) return;

    // Try to resolve the component definition
    const symbol = this.checker.getSymbolAtLocation(tagName);
    let resolvedPath: string | null = null;
    let calleeLine: number | null = null;
    let calleeKind: string = 'component';

    if (symbol) {
      const declarations = symbol.getDeclarations();
      if (declarations && declarations.length > 0) {
        const declaration = declarations[0];
        const sourceFile = declaration.getSourceFile();
        if (sourceFile) {
          resolvedPath = sourceFile.fileName;
          // Extract line number of the definition
          calleeLine = ts.getLineAndCharacterOfPosition(sourceFile, declaration.getStart()).line + 1;
          // Get more specific kind if possible (class component vs function component)
          const detectedKind = this.getSymbolKind(declaration);
          if (detectedKind !== 'unknown') {
            calleeKind = detectedKind;
          }
        }
      }
    }

    // Skip if resolved to node_modules
    if (resolvedPath && resolvedPath.includes('node_modules')) return;

    const callerContext = this.getCallerContext(node);
    const callerLine = ts.getLineAndCharacterOfPosition(this.sourceFile, node.getStart()).line + 1;
    const usage: TsUsage = {
      usageType: 'react_component',
      callerUri: this.pathToUri(this.sourceFile.fileName, callerLine),
      callerLine,
      callerSymbol: callerContext.symbol,
      callerKind: callerContext.kind,
      calleeUri: resolvedPath ? this.pathToUri(resolvedPath, calleeLine) : null,
      calleeSymbol: componentName,
      calleeLine,
      calleeKind,
      stringContext: null,
    };

    usages.push(usage);

    // Create bidirectional reference (component is used by caller)
    if (usage.calleeUri && usage.calleeLine) {
      usages.push(this.createBidirectionalUsage(usage));
    }
  }

  /**
   * Create bidirectional usage (inverse relationship)
   * For A calls B, also create B is called by A
   */
  private createBidirectionalUsage(originalUsage: TsUsage): TsUsage {
    // Map usage types to their inverse
    const inverseTypeMap: Record<string, string> = {
      'function_call': 'function_called_by',
      'hook_call': 'hook_called_by',
      'react_component': 'component_used_by',
    };

    const inverseType = inverseTypeMap[originalUsage.usageType] || `${originalUsage.usageType}_inverse`;

    return {
      usageType: inverseType,
      callerUri: originalUsage.calleeUri!,        // Swap: definition file becomes caller
      callerLine: originalUsage.calleeLine!, // At the definition line
      callerSymbol: originalUsage.calleeSymbol,    // The symbol itself
      callerKind: originalUsage.calleeKind,        // The kind of the symbol
      calleeUri: originalUsage.callerUri,          // Swap: usage file becomes callee
      calleeSymbol: originalUsage.callerSymbol,    // The method/component that uses it
      calleeLine: originalUsage.callerLine,  // At the usage line
      calleeKind: originalUsage.callerKind,        // The kind of the caller
      stringContext: `called_from:${originalUsage.callerUri}#L${originalUsage.callerLine}`,
    };
  }

  private handleExport(node: ts.ExportDeclaration | ts.ExportAssignment, usages: TsUsage[]): void {
    if (!this.sourceFile) return;

    if (ts.isExportDeclaration(node) && node.exportClause && ts.isNamedExports(node.exportClause)) {
      node.exportClause.elements.forEach(element => {
        const callerLine = ts.getLineAndCharacterOfPosition(this.sourceFile!, node.getStart()).line + 1;
        usages.push({
          usageType: 'export',
          callerUri: this.pathToUri(this.sourceFile!.fileName, callerLine),
          callerLine,
          callerSymbol: null,  // Exports are at module level
          callerKind: 'module',
          calleeUri: null,
          calleeSymbol: element.name.text,
          calleeLine: null,
          calleeKind: null,
          stringContext: null,
        });
      });
    }
  }

  /**
   * Resolve module name to absolute file path using TypeScript resolution
   */
  private resolveModuleName(moduleName: string): string | null {
    if (!this.sourceFile) return null;

    const resolvedModule = ts.resolveModuleName(
      moduleName,
      this.sourceFile.fileName,
      this.program!.getCompilerOptions(),
      ts.sys
    );

    if (resolvedModule.resolvedModule) {
      return resolvedModule.resolvedModule.resolvedFileName;
    }

    // For node_modules, return the module name as-is
    if (!moduleName.startsWith('.') && !moduleName.startsWith('/')) {
      return moduleName;
    }

    return null;
  }

  /**
   * Get the caller context (enclosing function/method/component) for a node
   */
  private getCallerContext(node: ts.Node): { symbol: string | null; kind: string | null } {
    let current = node.parent;
    while (current) {
      if (ts.isFunctionDeclaration(current) || ts.isMethodDeclaration(current)) {
        if (current.name && ts.isIdentifier(current.name)) {
          const symbol = current.name.text;
          const kind = ts.isMethodDeclaration(current) ? 'method' : 'function';
          return { symbol, kind };
        }
      }
      if (ts.isArrowFunction(current) || ts.isFunctionExpression(current)) {
        // Try to find variable name for arrow functions
        const parent = current.parent;
        if (parent && ts.isVariableDeclaration(parent) && ts.isIdentifier(parent.name)) {
          const symbol = parent.name.text;
          // Check if it's a React component (starts with uppercase)
          const kind = /^[A-Z]/.test(symbol) ? 'component' : 'function';
          return { symbol, kind };
        }
      }
      current = current.parent;
    }
    return { symbol: null, kind: null };
  }
}

// Endpoint to analyze a TypeScript file
app.post('/analyze', (req: Request, res: Response) => {
  try {
    const { filePath, fileContent, projectRoot } = req.body;

    // Validation
    if (!filePath || !fileContent) {
      return res.status(400).json({
        error: 'Missing required parameters',
        message: 'filePath and fileContent are required',
        service: 'TypeScriptASTService'
      });
    }

    if (typeof filePath !== 'string' || typeof fileContent !== 'string') {
      return res.status(400).json({
        error: 'Invalid parameter types',
        message: 'filePath and fileContent must be strings',
        service: 'TypeScriptASTService'
      });
    }

    // Analyze
    const analyzer = new TypeScriptAnalyzer();
    const usages = analyzer.analyze(
      filePath,
      fileContent,
      projectRoot || path.dirname(filePath)
    );

    res.json({
      usages,
      filePath,
      count: usages.length,
      service: 'TypeScriptASTService'
    });
  } catch (error: any) {
    console.error('Analysis error:', error);
    console.error('Stack:', error.stack);
    res.status(500).json({
      error: 'Analysis failed',
      message: error.message,
      service: 'TypeScriptASTService'
    });
  }
});

// Health check
app.get('/health', (req: Request, res: Response) => {
  res.json({ status: 'ok', service: 'TypeScriptASTService', port: PORT });
});

// Shutdown endpoint
app.post('/shutdown', (req: Request, res: Response) => {
  console.log('Shutdown request received');
  res.json({ status: 'shutting down', service: 'TypeScriptASTService' });

  // Give time for response to be sent
  setTimeout(() => {
    console.log('TypeScriptASTService shutting down...');
    process.exit(0);
  }, 500);
});

// Error handling middleware
app.use((err: Error, req: Request, res: Response, next: any) => {
  console.error('Server error:', err);
  res.status(500).json({
    error: 'Internal server error',
    message: err.message,
    service: 'TypeScriptASTService'
  });
});

const server = app.listen(PORT, () => {
  console.log(`TypeScriptASTService listening on port ${PORT}`);
});

// Graceful shutdown on SIGTERM/SIGINT
process.on('SIGTERM', () => {
  console.log('SIGTERM received, shutting down gracefully...');
  server.close(() => {
    console.log('Server closed');
    process.exit(0);
  });
});

process.on('SIGINT', () => {
  console.log('SIGINT received, shutting down gracefully...');
  server.close(() => {
    console.log('Server closed');
    process.exit(0);
  });
});
