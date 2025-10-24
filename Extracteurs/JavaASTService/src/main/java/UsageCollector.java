import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.ImportDeclaration;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.visitor.VoidVisitorAdapter;
import com.github.javaparser.resolution.declarations.ResolvedConstructorDeclaration;
import com.github.javaparser.resolution.declarations.ResolvedDeclaration;
import com.github.javaparser.resolution.declarations.ResolvedMethodDeclaration;
import com.github.javaparser.resolution.declarations.ResolvedReferenceTypeDeclaration;
import com.github.javaparser.resolution.types.ResolvedType;
import com.github.javaparser.symbolsolver.javassistmodel.JavassistMethodDeclaration;
import com.github.javaparser.symbolsolver.javaparsermodel.declarations.JavaParserClassDeclaration;
import com.github.javaparser.symbolsolver.javaparsermodel.declarations.JavaParserConstructorDeclaration;
import com.github.javaparser.symbolsolver.javaparsermodel.declarations.JavaParserInterfaceDeclaration;
import com.github.javaparser.symbolsolver.javaparsermodel.declarations.JavaParserMethodDeclaration;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.HashMap;
import java.util.List;
import java.util.ArrayList;

/**
 * Visitor that collects Java usages with complete taxonomy
 *
 * Usage types:
 * - java_field_injection: @Inject fields
 * - java_declaration: method/constructor definitions
 * - java_parameter: parameters in method/constructor definitions
 * - java_return_type: return types in method definitions
 * - java_method_call: method calls
 * - java_constructor_call: constructor calls (new)
 * - java_extends: class inheritance
 * - java_implements: interface implementation
 */
public class UsageCollector extends VoidVisitorAdapter<Void> {

    private String currentPackage;
    private String currentClassName;
    private String currentMethodName;
    private String module;
    private String filePath;
    private Map<String, String> importMap = new HashMap<>(); // simpleName -> FQN
    private List<String> domainPatterns;

    public List<Map<String, Object>> usages = new ArrayList<>();

    public UsageCollector(String packageName, String module, String filePath, List<String> domainPatterns) {
        this.currentPackage = packageName;
        this.module = module;
        this.filePath = filePath;
        this.domainPatterns = domainPatterns != null ? domainPatterns : new ArrayList<>();

        // Default pattern if none provided
        if (this.domainPatterns.isEmpty()) {
            this.domainPatterns.add("com.axelor.*");
        }
    }

    /**
     * Check if a FQN matches any of the configured domain patterns
     * Patterns support wildcards: "com.axelor.*" matches "com.axelor.apps.base"
     */
    private boolean matchesDomainPattern(String fqn) {
        if (fqn == null) return false;

        for (String pattern : domainPatterns) {
            if (pattern.endsWith(".*")) {
                // Wildcard pattern: check if FQN starts with the prefix
                String prefix = pattern.substring(0, pattern.length() - 2);
                if (fqn.startsWith(prefix)) {
                    return true;
                }
            } else {
                // Exact or contains match
                if (fqn.contains(pattern)) {
                    return true;
                }
            }
        }
        return false;
    }

    /**
     * Check if a FQN or any of its generic type parameters match the domain patterns
     * Examples:
     *   - "java.util.List<com.axelor.apps.base.db.Account>" -> true (Account matches)
     *   - "java.util.Map<java.lang.String, com.axelor.db.Model>" -> true (Model matches)
     *   - "java.util.List<java.lang.String>" -> false (neither List nor String match)
     */
    private boolean matchesDomainPatternOrGenerics(String fqn) {
        if (fqn == null) return false;

        // Check main type first
        if (matchesDomainPattern(fqn)) {
            return true;
        }

        // Extract and check generic type parameters
        // Example: "java.util.List<com.axelor.apps.Account>" -> extract "com.axelor.apps.Account"
        if (fqn.contains("<") && fqn.contains(">")) {
            int start = fqn.indexOf('<');
            int end = fqn.lastIndexOf('>');

            if (start < end) {
                String genericsContent = fqn.substring(start + 1, end);

                // Handle nested generics and comma-separated types
                // Split by comma, but respect nested brackets
                List<String> genericTypes = splitGenericTypes(genericsContent);

                for (String genericType : genericTypes) {
                    String trimmed = genericType.trim();

                    // Recursively check each generic type (handles nested generics)
                    if (matchesDomainPatternOrGenerics(trimmed)) {
                        return true;
                    }
                }
            }
        }

        return false;
    }

    /**
     * Split generic type parameters, respecting nested brackets
     * Example: "String, List<Account>, Map<String, Partner>"
     *   -> ["String", "List<Account>", "Map<String, Partner>"]
     */
    private List<String> splitGenericTypes(String genericsContent) {
        List<String> result = new ArrayList<>();
        StringBuilder current = new StringBuilder();
        int bracketDepth = 0;

        for (char c : genericsContent.toCharArray()) {
            if (c == '<') {
                bracketDepth++;
                current.append(c);
            } else if (c == '>') {
                bracketDepth--;
                current.append(c);
            } else if (c == ',' && bracketDepth == 0) {
                // Comma at depth 0 = separator
                if (current.length() > 0) {
                    result.add(current.toString().trim());
                    current = new StringBuilder();
                }
            } else {
                current.append(c);
            }
        }

        // Add last type
        if (current.length() > 0) {
            result.add(current.toString().trim());
        }

        return result;
    }

    @Override
    public void visit(CompilationUnit cu, Void arg) {
        // Extract imports for fallback resolution
        for (ImportDeclaration imp : cu.getImports()) {
            if (!imp.isAsterisk() && !imp.isStatic()) {
                String fqn = imp.getNameAsString();
                String simpleName = fqn.substring(fqn.lastIndexOf('.') + 1);
                importMap.put(simpleName, fqn);
            }
        }
        super.visit(cu, arg);
    }

    @Override
    public void visit(ClassOrInterfaceDeclaration n, Void arg) {
        String previousClassName = currentClassName;
        currentClassName = n.getNameAsString();
        int lineNum = n.getBegin().map(b -> b.line).orElse(-1);

        // Only capture extends/implements if this matches domain patterns
        String classFqn = getClassFQN();
        if (matchesDomainPattern(classFqn)) {
            // EXTENDS: class inheritance
            n.getExtendedTypes().forEach(extendedType -> {
                String parentFqn = null;
                ResolvedReferenceTypeDeclaration parentDecl = null;

                try {
                    ResolvedType resolvedType = extendedType.resolve();
                    parentFqn = resolvedType.describe();

                    // Try to get the type declaration for location resolution
                    if (resolvedType.isReferenceType()) {
                        parentDecl = resolvedType.asReferenceType().getTypeDeclaration().orElse(null);
                    }
                } catch (Exception e) {
                    // Resolution failed, fallback to simple name
                    parentFqn = extendedType.getNameAsString();
                }

                if (parentDecl != null) {
                    usages.add(createUsage(
                        "java_extends",
                        classFqn,
                        parentFqn,
                        lineNum,
                        parentDecl
                    ));
                } else {
                    usages.add(createUsage(
                        "java_extends",
                        classFqn,
                        parentFqn,
                        lineNum
                    ));
                }
            });

            // IMPLEMENTS: interface implementation (1 usage per interface)
            n.getImplementedTypes().forEach(implementedType -> {
                String interfaceFqn = null;
                ResolvedReferenceTypeDeclaration interfaceDecl = null;

                try {
                    ResolvedType resolvedType = implementedType.resolve();
                    interfaceFqn = resolvedType.describe();

                    // Try to get the type declaration for location resolution
                    if (resolvedType.isReferenceType()) {
                        interfaceDecl = resolvedType.asReferenceType().getTypeDeclaration().orElse(null);
                    }
                } catch (Exception e) {
                    // Resolution failed, fallback to simple name
                    interfaceFqn = implementedType.getNameAsString();
                }

                if (interfaceDecl != null) {
                    usages.add(createUsage(
                        "java_implements",
                        classFqn,
                        interfaceFqn,
                        lineNum,
                        interfaceDecl
                    ));
                } else {
                    usages.add(createUsage(
                        "java_implements",
                        classFqn,
                        interfaceFqn,
                        lineNum
                    ));
                }
            });
        }

        super.visit(n, arg);

        currentClassName = previousClassName;
    }

    @Override
    public void visit(MethodDeclaration n, Void arg) {
        String previousMethodName = currentMethodName;
        currentMethodName = n.getNameAsString();
        int lineNum = n.getBegin().map(b -> b.line).orElse(-1);

        // 1. DEFINITION: java_declaration
        usages.add(createUsage(
            "java_declaration",
            getClassFQN(),
            getMethodFQN(),
            lineNum
        ));

        // 2. RETURN TYPE: java_return_type
        try {
            ResolvedType resolvedReturn = n.getType().resolve();
            String returnTypeFqn = resolvedReturn.describe();

            if (matchesDomainPatternOrGenerics(returnTypeFqn)) {
                // Try to resolve the type declaration for location resolution
                ResolvedReferenceTypeDeclaration typeDecl = null;
                if (resolvedReturn.isReferenceType()) {
                    typeDecl = resolvedReturn.asReferenceType().getTypeDeclaration().orElse(null);
                }

                Map<String, Object> forwardUsage;
                if (typeDecl != null) {
                    forwardUsage = createUsage(
                        "java_return_type",
                        getMethodFQN(),
                        returnTypeFqn,
                        lineNum,
                        typeDecl
                    );
                } else {
                    forwardUsage = createUsage(
                        "java_return_type",
                        getMethodFQN(),
                        returnTypeFqn,
                        lineNum
                    );
                }
                usages.add(forwardUsage);

                // Create bidirectional usages for matching generic types
                // Example: List<ImportFieldMapping> -> create ImportFieldMapping is returned by method
                createInverseUsagesForMatchingGenerics(
                    forwardUsage,
                    resolvedReturn,
                    "java_return_type_of",
                    usages
                );
            }
        } catch (Exception e) {
            // Resolution failed, skip
        }

        // 3. PARAMETERS: java_parameter
        n.getParameters().forEach(param -> {
            try {
                ResolvedType resolvedType = param.getType().resolve();
                String paramTypeFqn = resolvedType.describe();

                // DEBUG: Log all parameter resolutions
                System.out.println("DEBUG [MethodDeclaration] Parameter: " + param.getNameAsString() +
                    " Type: " + param.getType() +
                    " Resolved: " + paramTypeFqn +
                    " in " + getMethodFQN());

                if (matchesDomainPatternOrGenerics(paramTypeFqn)) {
                    // Try to resolve the type declaration
                    ResolvedReferenceTypeDeclaration typeDecl =
                        resolvedType.asReferenceType().getTypeDeclaration().orElse(null);

                    Map<String, Object> forwardUsage;
                    if (typeDecl != null) {
                        forwardUsage = createUsage(
                            "java_parameter",
                            getMethodFQN(),
                            paramTypeFqn,
                            lineNum,
                            typeDecl
                        );
                    } else {
                        forwardUsage = createUsage(
                            "java_parameter",
                            getMethodFQN(),
                            paramTypeFqn,
                            lineNum
                        );
                    }
                    usages.add(forwardUsage);

                    // Create bidirectional usage (B is parameter of A)
                    Map<String, Object> inverseUsage = createBidirectionalUsage(forwardUsage, "java_parameter_of");
                    usages.add(inverseUsage);

                    System.out.println("DEBUG [MethodDeclaration] ✓ Parameter captured: " + paramTypeFqn);
                }
            } catch (Exception e) {
                // Resolution failed - try fallback using imports
                String typeName = param.getType().asString();
                String paramTypeFqn = null;

                // Try to find in imports
                if (importMap.containsKey(typeName)) {
                    paramTypeFqn = importMap.get(typeName);
                } else if (Character.isUpperCase(typeName.charAt(0))) {
                    // Not in imports, assume same package
                    paramTypeFqn = currentPackage.isEmpty() ? typeName : currentPackage + "." + typeName;
                }

                System.err.println("DEBUG [MethodDeclaration] ✗ Failed to resolve parameter: " + param.getNameAsString() +
                    " Type: " + typeName +
                    " in " + getMethodFQN() +
                    " Error: " + e.getClass().getSimpleName() + ": " + e.getMessage());

                // If we found a FQN and it contains "com.axelor", create usage anyway
                if (paramTypeFqn != null && matchesDomainPattern(paramTypeFqn)) {
                    System.err.println("DEBUG [MethodDeclaration] → Fallback resolution: " + paramTypeFqn);

                    Map<String, Object> forwardUsage = createUsage(
                        "java_parameter",
                        getMethodFQN(),
                        paramTypeFqn,
                        lineNum
                    );
                    usages.add(forwardUsage);

                    // Create bidirectional usage
                    Map<String, Object> inverseUsage = createBidirectionalUsage(forwardUsage, "java_parameter_of");
                    usages.add(inverseUsage);

                    System.err.println("DEBUG [MethodDeclaration] ✓ Parameter captured via fallback: " + paramTypeFqn);
                }
            }
        });

        super.visit(n, arg);

        currentMethodName = previousMethodName;
    }

    @Override
    public void visit(ConstructorDeclaration n, Void arg) {
        String previousMethodName = currentMethodName;
        currentMethodName = "<init>";
        int lineNum = n.getBegin().map(b -> b.line).orElse(-1);

        // 1. DEFINITION: java_declaration
        usages.add(createUsage(
            "java_declaration",
            getClassFQN(),
            getConstructorFQN(),
            lineNum
        ));

        // 2. PARAMETERS: java_parameter
        n.getParameters().forEach(param -> {
            try {
                ResolvedType resolvedType = param.getType().resolve();
                String paramTypeFqn = resolvedType.describe();

                if (matchesDomainPatternOrGenerics(paramTypeFqn)) {
                    // Try to resolve the type declaration
                    ResolvedReferenceTypeDeclaration typeDecl =
                        resolvedType.asReferenceType().getTypeDeclaration().orElse(null);

                    Map<String, Object> forwardUsage;
                    if (typeDecl != null) {
                        forwardUsage = createUsage(
                            "java_parameter",
                            getConstructorFQN(),
                            paramTypeFqn,
                            lineNum,
                            typeDecl
                        );
                    } else {
                        forwardUsage = createUsage(
                            "java_parameter",
                            getConstructorFQN(),
                            paramTypeFqn,
                            lineNum
                        );
                    }
                    usages.add(forwardUsage);

                    // Create bidirectional usage (B is parameter of A)
                    Map<String, Object> inverseUsage = createBidirectionalUsage(forwardUsage, "java_parameter_of");
                    usages.add(inverseUsage);
                }
            } catch (Exception e) {
                // Resolution failed - try fallback using imports
                String typeName = param.getType().asString();
                String paramTypeFqn = null;

                // Try to find in imports
                if (importMap.containsKey(typeName)) {
                    paramTypeFqn = importMap.get(typeName);
                } else if (Character.isUpperCase(typeName.charAt(0))) {
                    // Not in imports, assume same package
                    paramTypeFqn = currentPackage.isEmpty() ? typeName : currentPackage + "." + typeName;
                }

                // If we found a FQN and it contains "com.axelor", create usage anyway
                if (paramTypeFqn != null && matchesDomainPattern(paramTypeFqn)) {
                    Map<String, Object> forwardUsage = createUsage(
                        "java_parameter",
                        getConstructorFQN(),
                        paramTypeFqn,
                        lineNum
                    );
                    usages.add(forwardUsage);

                    // Create bidirectional usage
                    Map<String, Object> inverseUsage = createBidirectionalUsage(forwardUsage, "java_parameter_of");
                    usages.add(inverseUsage);
                }
            }
        });

        super.visit(n, arg);

        currentMethodName = previousMethodName;
    }

    @Override
    public void visit(FieldDeclaration n, Void arg) {
        super.visit(n, arg);

        // Check if it's an @Inject field
        boolean isInjected = n.getAnnotations().stream()
            .anyMatch(ann -> ann.getNameAsString().equals("Inject"));

        if (!isInjected) {
            return;
        }

        n.getVariables().forEach(var -> {
            try {
                ResolvedType resolvedType = n.getElementType().resolve();
                String fqn = resolvedType.describe();

                if (matchesDomainPattern(fqn)) {
                    int lineNum = n.getBegin().map(b -> b.line).orElse(-1);

                    usages.add(createUsage(
                        "java_field_injection",
                        getClassFQN(),
                        fqn,
                        lineNum
                    ));
                }
            } catch (Exception e) {
                // Resolution failed, skip
            }
        });
    }

    @Override
    public void visit(MethodCallExpr n, Void arg) {
        super.visit(n, arg);

        try {
            ResolvedMethodDeclaration resolved = n.resolve();
            String declaringType = resolved.declaringType().getQualifiedName();

            if (matchesDomainPattern(declaringType)) {
                int lineNum = n.getBegin().map(b -> b.line).orElse(-1);

                // Create forward usage (A calls B) with resolved location
                Map<String, Object> forwardUsage = createUsage(
                    "java_method_call",
                    getSourceFQN(),
                    resolved.getQualifiedName(),
                    lineNum,
                    resolved
                );
                usages.add(forwardUsage);

                // Create bidirectional usage (B is called by A)
                Map<String, Object> inverseUsage = createBidirectionalUsage(forwardUsage, "java_method_called_by");
                usages.add(inverseUsage);
            }
        } catch (Exception e) {
            // Resolution failed, skip
        }
    }

    @Override
    public void visit(ObjectCreationExpr n, Void arg) {
        super.visit(n, arg);

        try {
            ResolvedType resolvedType = n.getType().resolve();
            String fqn = resolvedType.describe();

            if (matchesDomainPattern(fqn)) {
                int lineNum = n.getBegin().map(b -> b.line).orElse(-1);

                // Try to resolve the constructor declaration
                Map<String, Object> forwardUsage;
                try {
                    ResolvedConstructorDeclaration constructor = n.resolve();
                    forwardUsage = createUsage(
                        "java_constructor_call",
                        getSourceFQN(),
                        fqn,
                        lineNum,
                        constructor
                    );
                } catch (Exception e) {
                    // Constructor resolution failed, try type declaration
                    try {
                        ResolvedReferenceTypeDeclaration typeDecl =
                            resolvedType.asReferenceType().getTypeDeclaration().orElse(null);
                        if (typeDecl != null) {
                            forwardUsage = createUsage(
                                "java_constructor_call",
                                getSourceFQN(),
                                fqn,
                                lineNum,
                                typeDecl
                            );
                        } else {
                            forwardUsage = createUsage(
                                "java_constructor_call",
                                getSourceFQN(),
                                fqn,
                                lineNum
                            );
                        }
                    } catch (Exception e2) {
                        forwardUsage = createUsage(
                            "java_constructor_call",
                            getSourceFQN(),
                            fqn,
                            lineNum
                        );
                    }
                }
                usages.add(forwardUsage);

                // Create bidirectional usage (B's constructor is called by A)
                Map<String, Object> inverseUsage = createBidirectionalUsage(forwardUsage, "java_constructor_called_by");
                usages.add(inverseUsage);
            }
        } catch (Exception e) {
            // Resolution failed, skip
        }
    }

    /**
     * Create inverse usages for generic types that match domain pattern
     * Example: For List<ImportFieldMapping>, create ImportFieldMapping is returned by method
     */
    private void createInverseUsagesForMatchingGenerics(
        Map<String, Object> forwardUsage,
        ResolvedType resolvedType,
        String inverseUsageType,
        List<Map<String, Object>> usages
    ) {
        try {
            // Get type parameters from resolved type
            if (!resolvedType.isReferenceType()) {
                return;
            }

            List<ResolvedType> typeParameters = resolvedType.asReferenceType().typeParametersValues();

            // Create inverse usage for each type parameter that matches domain pattern
            for (ResolvedType typeParam : typeParameters) {
                String genericTypeFqn = typeParam.describe();

                // Check if this type parameter matches domain pattern
                if (!matchesDomainPattern(genericTypeFqn)) {
                    // Recursively check nested generics
                    createInverseUsagesForMatchingGenerics(forwardUsage, typeParam, inverseUsageType, usages);
                    continue;
                }

                // Try to resolve the generic type to get its location
                try {
                    ResolvedReferenceTypeDeclaration genericDecl = null;
                    if (typeParam.isReferenceType()) {
                        genericDecl = typeParam.asReferenceType().getTypeDeclaration().orElse(null);
                    }

                    Map<String, Object> inverseUsage = new LinkedHashMap<>();
                    inverseUsage.put("usageType", inverseUsageType);

                    // Caller = the generic type (e.g., ImportFieldMapping)
                    if (genericDecl != null) {
                        String[] location = resolveDeclarationLocation(genericDecl);
                        String calleeFile = location[0];
                        String calleeLine = location[1];

                        if (calleeFile != null && calleeLine != null) {
                            int lineNum = Integer.parseInt(calleeLine);
                            inverseUsage.put("callerUri", pathToUri(calleeFile, lineNum));
                            inverseUsage.put("callerLine", lineNum);
                        } else {
                            inverseUsage.put("callerUri", null);
                            inverseUsage.put("callerLine", null);
                        }
                    } else {
                        inverseUsage.put("callerUri", null);
                        inverseUsage.put("callerLine", null);
                    }

                    inverseUsage.put("callerSymbol", extractMethodName(genericTypeFqn));
                    inverseUsage.put("callerKind", "class");
                    inverseUsage.put("caller_fqn", genericTypeFqn);

                    // Callee = the method from forward usage
                    inverseUsage.put("calleeUri", forwardUsage.get("callerUri"));
                    inverseUsage.put("calleeLine", forwardUsage.get("callerLine"));
                    inverseUsage.put("calleeSymbol", forwardUsage.get("callerSymbol"));
                    inverseUsage.put("calleeKind", forwardUsage.get("callerKind"));
                    inverseUsage.put("callee_fqn", forwardUsage.get("caller_fqn"));

                    inverseUsage.put("module", module);

                    usages.add(inverseUsage);

                    // Also recursively check this type's generics
                    createInverseUsagesForMatchingGenerics(forwardUsage, typeParam, inverseUsageType, usages);
                } catch (Exception e) {
                    // Could not resolve generic type, skip
                }
            }
        } catch (Exception e) {
            // Extraction failed, skip
        }
    }

    /**
     * Resolve the source file and line number from a resolved declaration
     */
    private String[] resolveDeclarationLocation(ResolvedDeclaration decl) {
        try {
            // Try to get the AST node from the resolved declaration
            if (decl instanceof JavassistMethodDeclaration) {
                // Javassist declarations (from compiled classes) don't have source info
                return new String[]{null, null};
            }

            // For JavaParser-backed method declarations
            if (decl instanceof JavaParserMethodDeclaration) {
                JavaParserMethodDeclaration jpDecl = (JavaParserMethodDeclaration) decl;

                MethodDeclaration astNode = jpDecl.getWrappedNode();
                if (astNode != null && astNode.getBegin().isPresent()) {
                    String sourceFile = astNode.findCompilationUnit()
                        .flatMap(cu -> cu.getStorage())
                        .map(storage -> storage.getPath().toString())
                        .orElse(null);

                    int line = astNode.getBegin().get().line;
                    return new String[]{sourceFile, String.valueOf(line)};
                }
            }

            // For JavaParser-backed constructor declarations
            if (decl instanceof JavaParserConstructorDeclaration) {
                JavaParserConstructorDeclaration jpDecl = (JavaParserConstructorDeclaration) decl;

                ConstructorDeclaration astNode = jpDecl.getWrappedNode();
                if (astNode != null && astNode.getBegin().isPresent()) {
                    String sourceFile = astNode.findCompilationUnit()
                        .flatMap(cu -> cu.getStorage())
                        .map(storage -> storage.getPath().toString())
                        .orElse(null);

                    int line = astNode.getBegin().get().line;
                    return new String[]{sourceFile, String.valueOf(line)};
                }
            }

            // For JavaParser-backed class declarations
            if (decl instanceof JavaParserClassDeclaration) {
                JavaParserClassDeclaration jpDecl = (JavaParserClassDeclaration) decl;

                ClassOrInterfaceDeclaration astNode = jpDecl.getWrappedNode();
                if (astNode != null && astNode.getBegin().isPresent()) {
                    String sourceFile = astNode.findCompilationUnit()
                        .flatMap(cu -> cu.getStorage())
                        .map(storage -> storage.getPath().toString())
                        .orElse(null);

                    int line = astNode.getBegin().get().line;
                    return new String[]{sourceFile, String.valueOf(line)};
                }
            }

            // For JavaParser-backed interface declarations
            if (decl instanceof JavaParserInterfaceDeclaration) {
                JavaParserInterfaceDeclaration jpDecl = (JavaParserInterfaceDeclaration) decl;

                ClassOrInterfaceDeclaration astNode = jpDecl.getWrappedNode();
                if (astNode != null && astNode.getBegin().isPresent()) {
                    String sourceFile = astNode.findCompilationUnit()
                        .flatMap(cu -> cu.getStorage())
                        .map(storage -> storage.getPath().toString())
                        .orElse(null);

                    int line = astNode.getBegin().get().line;
                    return new String[]{sourceFile, String.valueOf(line)};
                }
            }
        } catch (Exception e) {
            // Resolution failed
        }

        return new String[]{null, null};
    }

    /**
     * Creates a usage with standardized cross-language structure
     */
    private Map<String, Object> createUsage(
        String usageType,
        String callerFqn,
        String calleeFqn,
        int lineNumber
    ) {
        Map<String, Object> usage = new LinkedHashMap<>();

        // Standardized fields (cross-language compatible)
        usage.put("usageType", usageType);
        usage.put("callerUri", pathToUri(filePath, lineNumber));
        usage.put("callerLine", lineNumber);
        usage.put("callerSymbol", extractMethodName(callerFqn));
        usage.put("callerKind", getCallerKind(usageType, callerFqn));
        usage.put("caller_fqn", callerFqn);

        // For definitions, the callee is defined at the same location
        if ("java_declaration".equals(usageType)) {
            usage.put("calleeUri", pathToUri(filePath, lineNumber));
            usage.put("calleeLine", lineNumber);
        } else {
            usage.put("calleeUri", null);
            usage.put("calleeLine", null);
        }

        usage.put("calleeSymbol", extractMethodName(calleeFqn));
        usage.put("calleeKind", getCalleeKind(usageType, calleeFqn));
        usage.put("callee_fqn", calleeFqn);

        // Java-specific metadata
        usage.put("module", module);

        return usage;
    }

    /**
     * Creates a usage with callee location resolved
     */
    private Map<String, Object> createUsage(
        String usageType,
        String callerFqn,
        String calleeFqn,
        int lineNumber,
        ResolvedDeclaration calleeDecl
    ) {
        Map<String, Object> usage = new LinkedHashMap<>();

        // Resolve callee location
        String[] location = resolveDeclarationLocation(calleeDecl);
        String calleeFile = location[0];
        String calleeLine = location[1];

        // Standardized fields (cross-language compatible)
        usage.put("usageType", usageType);
        usage.put("callerUri", pathToUri(filePath, lineNumber));
        usage.put("callerLine", lineNumber);
        usage.put("callerSymbol", extractMethodName(callerFqn));
        usage.put("callerKind", getCallerKind(usageType, callerFqn));
        usage.put("caller_fqn", callerFqn);

        // Add calleeUri with line number if both file and line are available
        if (calleeFile != null && calleeLine != null) {
            int calleeLineNum = Integer.parseInt(calleeLine);
            usage.put("calleeUri", pathToUri(calleeFile, calleeLineNum));
            usage.put("calleeLine", calleeLineNum);
        } else if (calleeFile != null) {
            usage.put("calleeUri", pathToUri(calleeFile));
            usage.put("calleeLine", calleeLine != null ? Integer.parseInt(calleeLine) : null);
        } else {
            usage.put("calleeUri", null);
            usage.put("calleeLine", null);
        }

        usage.put("calleeSymbol", extractMethodName(calleeFqn));
        usage.put("calleeKind", getCalleeKind(usageType, calleeFqn));
        usage.put("callee_fqn", calleeFqn);

        // Java-specific metadata
        usage.put("module", module);

        return usage;
    }

    /**
     * Create bidirectional usage (inverse relationship)
     * For A calls B, also create B is called by A
     */
    private Map<String, Object> createBidirectionalUsage(Map<String, Object> originalUsage, String inverseType) {
        Map<String, Object> inverseUsage = new LinkedHashMap<>();

        // Swap caller and callee
        inverseUsage.put("usageType", inverseType);
        inverseUsage.put("callerUri", originalUsage.get("calleeUri"));
        inverseUsage.put("callerLine", originalUsage.get("calleeLine"));

        // Special handling for constructors: extract <init> from FQN
        String callerFqn = (String) originalUsage.get("callee_fqn");
        String callerSymbol;
        if ("java_constructor_called_by".equals(inverseType) && "constructor".equals(originalUsage.get("calleeKind"))) {
            // For constructors, use <init> as the symbol
            callerSymbol = "<init>";
        } else {
            callerSymbol = (String) originalUsage.get("calleeSymbol");
        }

        inverseUsage.put("callerSymbol", callerSymbol);
        inverseUsage.put("callerKind", originalUsage.get("calleeKind"));
        inverseUsage.put("caller_fqn", callerFqn);
        inverseUsage.put("calleeUri", originalUsage.get("callerUri"));
        inverseUsage.put("calleeSymbol", originalUsage.get("callerSymbol"));
        inverseUsage.put("calleeLine", originalUsage.get("callerLine"));
        inverseUsage.put("calleeKind", originalUsage.get("callerKind"));
        inverseUsage.put("callee_fqn", originalUsage.get("caller_fqn"));

        // Java-specific metadata (same as original)
        inverseUsage.put("module", originalUsage.getOrDefault("module", "unknown"));

        return inverseUsage;
    }

    /**
     * Convert file path to URI format (file:///)
     */
    private String pathToUri(String filePath) {
        if (filePath == null) return null;

        // Normalize path separators to forward slashes
        String normalized = filePath.replace('\\', '/');

        // Add file:/// prefix
        if (normalized.startsWith("file:///")) {
            return normalized;
        }

        // Handle absolute paths
        if (normalized.matches("^[a-zA-Z]:/.*")) {
            // Windows absolute path: C:/... -> file:///C:/...
            return "file:///" + normalized;
        } else if (normalized.startsWith("/")) {
            // Unix absolute path: /... -> file:///...
            return "file://" + normalized;
        }

        // Relative paths (shouldn't happen, but handle gracefully)
        return "file:///" + normalized;
    }

    /**
     * Convert file path to URI format with line number (file:///path:line)
     */
    private String pathToUri(String filePath, int lineNumber) {
        String uri = pathToUri(filePath);
        if (uri == null) return null;
        return uri + ":" + lineNumber;
    }

    /**
     * Extract method name from fully qualified name
     * e.g., "com.example.MyClass.myMethod" -> "myMethod"
     * e.g., "java.nio.file.SimpleFileVisitor<java.nio.file.Path>" -> "SimpleFileVisitor"
     */
    private String extractMethodName(String fqn) {
        if (fqn == null) return null;

        // Remove generics first (everything after '<')
        int genericStart = fqn.indexOf('<');
        String fqnWithoutGenerics = genericStart >= 0 ? fqn.substring(0, genericStart) : fqn;

        // Extract simple name after last dot
        int lastDot = fqnWithoutGenerics.lastIndexOf('.');
        return lastDot >= 0 ? fqnWithoutGenerics.substring(lastDot + 1) : fqnWithoutGenerics;
    }

    /**
     * Determine the kind of caller based on usage type and FQN
     */
    private String getCallerKind(String usageType, String callerFqn) {
        // Check if it's a constructor
        if (callerFqn != null && callerFqn.contains("<init>")) {
            return "constructor";
        }

        switch (usageType) {
            case "java_declaration":
            case "java_return_type":
            case "java_parameter":
                return "method";
            case "java_method_call":
            case "java_constructor_call":
                return currentMethodName != null ? "method" : "class";
            case "java_extends":
            case "java_implements":
            case "java_field_injection":
                return "class";
            default:
                return "unknown";
        }
    }

    /**
     * Determine the kind of callee based on usage type and FQN
     */
    private String getCalleeKind(String usageType, String calleeFqn) {
        // Check if it's a constructor
        if (calleeFqn != null && calleeFqn.contains("<init>")) {
            return "constructor";
        }

        switch (usageType) {
            case "java_method_call":
                return "method";
            case "java_constructor_call":
                return "constructor";
            case "java_extends":
                return "class";
            case "java_implements":
                return "interface";
            case "java_field_injection":
            case "java_parameter":
            case "java_return_type":
                return "class";
            default:
                return "unknown";
        }
    }

    private String getSourceFQN() {
        if (currentPackage == null) {
            return "UNKNOWN";
        }

        StringBuilder sb = new StringBuilder(currentPackage);

        if (currentClassName != null) {
            sb.append(".").append(currentClassName);
        }

        if (currentMethodName != null) {
            sb.append(".").append(currentMethodName);
        }

        return sb.toString();
    }

    private String getClassFQN() {
        if (currentPackage == null || currentClassName == null) {
            return "UNKNOWN";
        }
        return currentPackage + "." + currentClassName;
    }

    private String getMethodFQN() {
        if (currentPackage == null || currentClassName == null || currentMethodName == null) {
            return "UNKNOWN";
        }
        return currentPackage + "." + currentClassName + "." + currentMethodName;
    }

    private String getConstructorFQN() {
        if (currentPackage == null || currentClassName == null) {
            return "UNKNOWN";
        }
        return currentPackage + "." + currentClassName + ".<init>";
    }
}
