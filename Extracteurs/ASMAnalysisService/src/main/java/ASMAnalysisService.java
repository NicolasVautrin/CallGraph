import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import spark.Request;
import spark.Response;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.*;
import java.util.stream.Collectors;

import static spark.Spark.*;

/**
 * ASM Analysis Service - REST API for analyzing Java bytecode
 *
 * This service uses ASM to analyze .class files and extract:
 * - Node types (class, interface, enum)
 * - Method calls
 * - Inheritance relationships
 * - Field types
 *
 * Port: 8766
 */
public class ASMAnalysisService {
    private static final Logger logger = LoggerFactory.getLogger(ASMAnalysisService.class);
    private static final ObjectMapper mapper = new ObjectMapper();
    private static final int PORT = 8766;

    public static void main(String[] args) {
        port(PORT);

        // Health check
        get("/health", (req, res) -> {
            res.type("application/json");
            Map<String, Object> health = new HashMap<>();
            health.put("status", "ok");
            health.put("service", "ASMAnalysisService");
            health.put("version", "1.0.0");
            return mapper.writeValueAsString(health);
        });

        // Main analysis endpoint
        post("/analyze", ASMAnalysisService::analyze);

        // Lightweight indexing endpoint
        post("/index", ASMAnalysisService::index);

        // Batch indexing endpoint
        post("/index/batch", ASMAnalysisService::indexBatch);

        // Shutdown endpoint
        post("/shutdown", (req, res) -> {
            res.type("application/json");
            Map<String, Object> response = new HashMap<>();
            response.put("status", "shutting down");
            logger.info("Shutdown request received, stopping service...");

            // Stop Spark in a separate thread to allow response to be sent
            new Thread(() -> {
                try {
                    Thread.sleep(500); // Give time for response to be sent
                    stop();
                    System.exit(0);
                } catch (InterruptedException e) {
                    logger.error("Error during shutdown", e);
                }
            }).start();

            return mapper.writeValueAsString(response);
        });

        // Error handling
        exception(Exception.class, (e, req, res) -> {
            res.status(500);
            res.type("application/json");
            Map<String, String> error = new HashMap<>();
            error.put("error", e.getMessage());
            error.put("type", e.getClass().getSimpleName());
            try {
                res.body(mapper.writeValueAsString(error));
            } catch (JsonProcessingException ex) {
                res.body("{\"error\": \"Internal error\"}");
            }
            e.printStackTrace();
        });

        logger.info("ASM Analysis Service started on port {}", PORT);
        logger.info("Endpoints:");
        logger.info("  GET  /health        - Health check");
        logger.info("  POST /analyze       - Analyze class files");
        logger.info("  POST /index         - Index symbols (single file)");
        logger.info("  POST /index/batch   - Index symbols (batch)");
    }

    /**
     * Analyze class files from given directories or files
     *
     * Request body (Option 1 - Package Roots):
     * {
     *   "packageRoots": ["/path/to/axelor-core-7.2.3", "/path/to/axelor-web-7.2.3"],
     *   "limit": 100  // optional
     * }
     *
     * Request body (Option 2 - Explicit Paths):
     * {
     *   "classDirs": ["/path/to/classes1", "/path/to/classes2"],
     *   "mapping": {
     *     "/path/to/classes1": "/path/to/sources1",
     *     "/path/to/classes2": "/path/to/sources2"
     *   },
     *   "limit": 100  // optional
     * }
     *
     * Request body (Option 3 - Individual Class Files):
     * {
     *   "classFiles": ["/path/to/MyClass.class", "/path/to/OtherClass.class"],
     *   "mapping": {
     *     "/path/to/classes": "/path/to/sources"
     *   },
     *   "domains": ["com.axelor.apps", "com.example"],  // optional: filter classes by FQN prefix
     *   "limit": 100  // optional
     * }
     *
     * Response:
     * {
     *   "nodes": [
     *     {
     *       "fqn": "com.example.MyClass",
     *       "nodeType": "class",
     *       "name": "MyClass",
     *       "modifiers": ["public", "final"],
     *       "isInterface": false,
     *       "isEnum": false
     *     }
     *   ],
     *   "edges": [
     *     {
     *       "edgeType": "call",
     *       "fromFqn": "com.example.MyClass.method()",
     *       "toFqn": "com.example.OtherClass.otherMethod()",
     *       "kind": "standard"
     *     }
     *   ]
     * }
     */
    private static String analyze(Request req, Response res) throws IOException {
        res.type("application/json");

        // Parse request
        @SuppressWarnings("unchecked")
        Map<String, Object> request = mapper.readValue(req.body(), Map.class);

        List<String> classDirs = new ArrayList<>();
        Map<String, String> mapping = new HashMap<>();
        List<Path> classFiles = new ArrayList<>();

        // Option 1: packageRoots (auto-discover classes/ and sources/)
        // Also extract packageName from path if not provided
        String autoDetectedPackageName = null;
        if (request.containsKey("packageRoots")) {
            @SuppressWarnings("unchecked")
            List<String> packageRoots = (List<String>) request.get("packageRoots");

            for (String packageRoot : packageRoots) {
                Path rootPath = Paths.get(packageRoot);

                // Extract package name from directory name (e.g., "axelor-core-7.2.6")
                if (autoDetectedPackageName == null) {
                    autoDetectedPackageName = rootPath.getFileName().toString();
                }

                Path classesPath = rootPath.resolve("classes");
                Path sourcesPath = rootPath.resolve("sources");

                if (Files.exists(classesPath) && Files.isDirectory(classesPath)) {
                    classDirs.add(classesPath.toString());

                    // Add mapping if sources exist
                    if (Files.exists(sourcesPath) && Files.isDirectory(sourcesPath)) {
                        mapping.put(classesPath.toString(), sourcesPath.toString());
                    }
                } else {
                    System.err.println("Warning: classes/ not found in " + packageRoot);
                }
            }
        }
        // Option 2: explicit classDirs (backward compatibility)
        else if (request.containsKey("classDirs")) {
            @SuppressWarnings("unchecked")
            List<String> explicitClassDirs = (List<String>) request.get("classDirs");
            classDirs.addAll(explicitClassDirs);

            if (request.containsKey("mapping")) {
                @SuppressWarnings("unchecked")
                Map<String, String> explicitMapping = (Map<String, String>) request.get("mapping");
                mapping.putAll(explicitMapping);
            }
        }
        // Option 3: explicit class files
        else if (request.containsKey("classFiles")) {
            @SuppressWarnings("unchecked")
            List<String> explicitClassFiles = (List<String>) request.get("classFiles");

            for (String classFilePath : explicitClassFiles) {
                Path path = Paths.get(classFilePath);
                if (Files.exists(path) && classFilePath.endsWith(".class")) {
                    classFiles.add(path);
                } else {
                    System.err.println("Warning: invalid class file: " + classFilePath);
                }
            }

            if (request.containsKey("mapping")) {
                @SuppressWarnings("unchecked")
                Map<String, String> explicitMapping = (Map<String, String>) request.get("mapping");
                mapping.putAll(explicitMapping);
            }
        } else {
            res.status(400);
            return mapper.writeValueAsString(Map.of("error", "Either packageRoots, classDirs, or classFiles is required"));
        }

        Integer limit = request.containsKey("limit") ?
            ((Number) request.get("limit")).intValue() : null;

        // Get domains for filtering (default to empty list)
        List<String> domains = new ArrayList<>();
        if (request.containsKey("domains")) {
            @SuppressWarnings("unchecked")
            List<String> domainsList = (List<String>) request.get("domains");
            domains.addAll(domainsList);
        }

        // Get packageName: use provided or auto-detected from packageRoots
        String packageName = request.containsKey("packageName") ?
            (String) request.get("packageName") : autoDetectedPackageName;

        // Collect all .class files if not already provided
        if (classFiles.isEmpty()) {
            if (classDirs.isEmpty()) {
                res.status(400);
                return mapper.writeValueAsString(Map.of("error", "No valid class directories or files found"));
            }

            for (String dirPath : classDirs) {
                Path dir = Paths.get(dirPath);
                if (Files.exists(dir) && Files.isDirectory(dir)) {
                    Files.walk(dir)
                        .filter(p -> p.toString().endsWith(".class"))
                        .forEach(classFiles::add);
                }
            }
        }

        // Apply limit if specified
        if (limit != null && classFiles.size() > limit) {
            classFiles = classFiles.subList(0, limit);
        }

        logger.info("Analyzing {} class files", classFiles.size());

        // Analyze files
        List<Map<String, Object>> nodes = new ArrayList<>();
        List<Map<String, Object>> edges = new ArrayList<>();

        for (Path classFile : classFiles) {
            try {
                ClassAnalyzer analyzer = new ClassAnalyzer(classFile);
                analyzer.analyze();

                nodes.addAll(analyzer.getNodes());
                edges.addAll(analyzer.getEdges());
            } catch (Exception e) {
                System.err.println("Failed to analyze " + classFile + ": " + e.getMessage());
            }
        }

        // Group data by class for readable structure
        Map<String, Map<String, Object>> classByFqn = new HashMap<>();

        // Step 1: Build class nodes
        for (Map<String, Object> node : nodes) {
            String nodeType = (String) node.get("nodeType");
            String fqn = (String) node.get("fqn");

            // Filter classes by domain
            if ("class".equals(nodeType) || "interface".equals(nodeType) || "enum".equals(nodeType)) {
                if (!matchesDomainFilter(fqn, domains)) {
                    continue; // Skip non-matching classes
                }

                Map<String, Object> classData = new HashMap<>();
                classData.put("fqn", fqn);
                classData.put("nodeType", nodeType);
                classData.put("modifiers", node.get("modifiers"));
                classData.put("isInterface", node.get("isInterface"));
                classData.put("isEnum", node.get("isEnum"));
                classData.put("isAbstract", node.get("isAbstract"));
                classData.put("methods", new ArrayList<Map<String, Object>>());
                classData.put("fields", new ArrayList<Map<String, Object>>());
                classData.put("inheritance", new ArrayList<Map<String, Object>>());

                classByFqn.put(fqn, classData);
            }
        }

        // Step 2: Add methods to their classes
        Map<String, Map<String, Object>> methodByFqn = new HashMap<>();
        for (Map<String, Object> node : nodes) {
            String nodeType = (String) node.get("nodeType");
            if ("method".equals(nodeType)) {
                String methodFqn = (String) node.get("fqn");

                // Extract class FQN from method FQN
                // Find the '.' before the method name, not in the parameter types
                int paramStart = methodFqn.indexOf('(');
                int methodSep = methodFqn.lastIndexOf('.', paramStart);
                if (methodSep == -1) continue;
                String classFqn = methodFqn.substring(0, methodSep);

                Map<String, Object> classData = classByFqn.get(classFqn);
                if (classData == null) continue; // Method's class was filtered out

                Map<String, Object> methodData = new HashMap<>();
                methodData.put("fqn", methodFqn);
                methodData.put("lineNumber", node.get("lineNumber"));
                methodData.put("modifiers", node.get("modifiers"));
                methodData.put("hasOverride", node.get("hasOverride"));
                methodData.put("isTransactional", node.get("isTransactional"));
                methodData.put("returnType", null);
                methodData.put("arguments", new ArrayList<String>());
                methodData.put("calls", new ArrayList<Map<String, Object>>());

                @SuppressWarnings("unchecked")
                List<Map<String, Object>> methods = (List<Map<String, Object>>) classData.get("methods");
                methods.add(methodData);

                methodByFqn.put(methodFqn, methodData);
            }
        }

        // Step 3: Process edges
        for (Map<String, Object> edge : edges) {
            String edgeType = (String) edge.get("edgeType");

            if ("inheritance".equals(edgeType)) {
                // Add to class inheritance
                String fromFqn = (String) edge.get("fromFqn");
                Map<String, Object> classData = classByFqn.get(fromFqn);
                if (classData != null) {
                    Map<String, Object> inhData = new HashMap<>();
                    inhData.put("fqn", edge.get("toFqn"));
                    inhData.put("kind", edge.get("kind"));

                    @SuppressWarnings("unchecked")
                    List<Map<String, Object>> inheritance = (List<Map<String, Object>>) classData.get("inheritance");
                    inheritance.add(inhData);
                }
            } else if ("call".equals(edgeType)) {
                // Add to method calls
                String fromFqn = (String) edge.get("fromFqn");
                Map<String, Object> methodData = methodByFqn.get(fromFqn);
                if (methodData != null) {
                    Map<String, Object> callData = new HashMap<>();
                    callData.put("toFqn", edge.get("toFqn"));
                    callData.put("kind", edge.get("kind"));
                    callData.put("lineNumber", edge.get("lineNumber"));

                    @SuppressWarnings("unchecked")
                    List<Map<String, Object>> calls = (List<Map<String, Object>>) methodData.get("calls");
                    calls.add(callData);
                }
            } else if ("member_of".equals(edgeType)) {
                String kind = (String) edge.get("kind");
                String fromFqn = (String) edge.get("fromFqn"); // The type
                String toFqn = (String) edge.get("toFqn");     // The container

                if ("return".equals(kind)) {
                    // Return type of a method
                    Map<String, Object> methodData = methodByFqn.get(toFqn);
                    if (methodData != null) {
                        methodData.put("returnType", fromFqn);
                    }
                } else if ("argument".equals(kind)) {
                    // Argument type of a method
                    Map<String, Object> methodData = methodByFqn.get(toFqn);
                    if (methodData != null) {
                        @SuppressWarnings("unchecked")
                        List<String> arguments = (List<String>) methodData.get("arguments");
                        arguments.add(fromFqn);
                    }
                } else if ("class".equals(kind)) {
                    // Field type of a class
                    // Extract class FQN from toFqn (which is the class)
                    Map<String, Object> classData = classByFqn.get(toFqn);
                    if (classData != null) {
                        Map<String, Object> fieldData = new HashMap<>();
                        fieldData.put("type", fromFqn);

                        @SuppressWarnings("unchecked")
                        List<Map<String, Object>> fields = (List<Map<String, Object>>) classData.get("fields");
                        fields.add(fieldData);
                    }
                }
            }
        }

        // Build final response
        Map<String, Object> response = new HashMap<>();
        response.put("success", true);
        response.put("classes", new ArrayList<>(classByFqn.values()));

        return mapper.writeValueAsString(response);
    }

    /**
     * Check if a class FQN matches any of the domain filters
     * Returns true if domains is empty OR if fqn starts with at least one domain
     */
    private static boolean matchesDomainFilter(String fqn, List<String> domains) {
        if (domains == null || domains.isEmpty()) {
            return true;
        }
        for (String domain : domains) {
            if (fqn.startsWith(domain)) {
                return true;
            }
        }
        return false;
    }

    /**
     * Lightweight indexing endpoint - returns grouped class symbols (excludes enums)
     *
     * Request body:
     * {
     *   "classFile": "/path/to/MyClass.class"
     * }
     *
     * Response (class/interface):
     * {
     *   "success": true,
     *   "class_fqn": "com.example.MyClass",
     *   "is_entity": false,
     *   "symbols": [
     *     {"fqn": "com.example.MyClass", "nodeType": "class", "line": 10, "isEntity": false},
     *     {"fqn": "com.example.MyClass.myMethod()", "nodeType": "method", "line": 15, "isEntity": false}
     *   ]
     * }
     *
     * Response (enum - skipped):
     * {
     *   "success": true,
     *   "skipped": true,
     *   "reason": "enum"
     * }
     */
    private static String index(Request req, Response res) throws IOException {
        res.type("application/json");

        // Parse request
        @SuppressWarnings("unchecked")
        Map<String, Object> request = mapper.readValue(req.body(), Map.class);

        // classFile is required
        if (!request.containsKey("classFile")) {
            res.status(400);
            return mapper.writeValueAsString(Map.of("error", "classFile is required"));
        }

        String classFilePath = (String) request.get("classFile");

        // Validate class file
        Path classFile = Paths.get(classFilePath);
        if (!Files.exists(classFile) || !classFilePath.endsWith(".class")) {
            res.status(400);
            return mapper.writeValueAsString(Map.of("error", "Invalid class file: " + classFilePath));
        }

        logger.info("Indexing class file: {}", classFile);

        // Extract symbols
        String classFqn = null;
        boolean isEntity = false;
        List<Map<String, Object>> symbols = new ArrayList<>();

        try {
            ClassAnalyzer analyzer = new ClassAnalyzer(classFile);
            analyzer.analyze();

            // First pass: find the class node and check if it's an enum
            for (Map<String, Object> node : analyzer.getNodes()) {
                String nodeType = (String) node.get("nodeType");

                if ("enum".equals(nodeType)) {
                    // Skip enums entirely
                    Map<String, Object> response = new HashMap<>();
                    response.put("success", true);
                    response.put("skipped", true);
                    response.put("reason", "enum");
                    return mapper.writeValueAsString(response);
                }

                if ("class".equals(nodeType) || "interface".equals(nodeType)) {
                    classFqn = (String) node.get("fqn");
                    isEntity = node.get("isEntity") != null && (Boolean) node.get("isEntity");
                    break;
                }
            }

            // Second pass: collect all symbols (class + methods)
            for (Map<String, Object> node : analyzer.getNodes()) {
                String nodeType = (String) node.get("nodeType");
                String fqn = (String) node.get("fqn");

                Map<String, Object> symbol = new HashMap<>();
                symbol.put("fqn", fqn);
                symbol.put("nodeType", nodeType);
                symbol.put("line", node.get("lineNumber"));
                symbol.put("isEntity", isEntity); // Use class's isEntity for all symbols
                symbols.add(symbol);
            }
        } catch (Exception e) {
            res.status(500);
            return mapper.writeValueAsString(Map.of("error", "Failed to index " + classFile + ": " + e.getMessage()));
        }

        // Build grouped response
        Map<String, Object> response = new HashMap<>();
        response.put("success", true);
        response.put("class_fqn", classFqn);
        response.put("is_entity", isEntity);
        response.put("symbols", symbols);

        return mapper.writeValueAsString(response);
    }

    /**
     * Batch indexing endpoint - indexes multiple class files in one request
     *
     * Request body:
     * {
     *   "classFiles": ["/path/to/Class1.class", "/path/to/Class2.class", ...]
     * }
     *
     * Response:
     * {
     *   "success": true,
     *   "results": [
     *     {
     *       "success": true,
     *       "class_fqn": "com.example.Class1",
     *       "is_entity": false,
     *       "symbols": [...]
     *     },
     *     {
     *       "success": true,
     *       "skipped": true,
     *       "reason": "enum"
     *     },
     *     {
     *       "success": false,
     *       "error": "Failed to index: ..."
     *     }
     *   ]
     * }
     */
    private static String indexBatch(Request req, Response res) throws IOException {
        res.type("application/json");

        // Parse request
        @SuppressWarnings("unchecked")
        Map<String, Object> request = mapper.readValue(req.body(), Map.class);

        // classFiles is required
        if (!request.containsKey("classFiles")) {
            res.status(400);
            return mapper.writeValueAsString(Map.of("error", "classFiles array is required"));
        }

        @SuppressWarnings("unchecked")
        List<String> classFilePaths = (List<String>) request.get("classFiles");

        if (classFilePaths == null || classFilePaths.isEmpty()) {
            res.status(400);
            return mapper.writeValueAsString(Map.of("error", "classFiles array cannot be empty"));
        }

        logger.info("Batch indexing {} class files", classFilePaths.size());

        List<Map<String, Object>> results = new ArrayList<>();

        // Process each class file
        for (String classFilePath : classFilePaths) {
            Map<String, Object> result = new HashMap<>();

            try {
                // Validate class file
                Path classFile = Paths.get(classFilePath);
                if (!Files.exists(classFile) || !classFilePath.endsWith(".class")) {
                    result.put("success", false);
                    result.put("error", "Invalid class file: " + classFilePath);
                    results.add(result);
                    continue;
                }

                // Extract symbols
                String classFqn = null;
                boolean isEntity = false;
                boolean isEnum = false;
                List<Map<String, Object>> symbols = new ArrayList<>();

                ClassAnalyzer analyzer = new ClassAnalyzer(classFile);
                analyzer.analyze();

                // First pass: find the class node and check if it's an enum
                for (Map<String, Object> node : analyzer.getNodes()) {
                    String nodeType = (String) node.get("nodeType");

                    if ("enum".equals(nodeType)) {
                        // Skip enums entirely
                        isEnum = true;
                        break;
                    }

                    if ("class".equals(nodeType) || "interface".equals(nodeType)) {
                        classFqn = (String) node.get("fqn");
                        isEntity = node.get("isEntity") != null && (Boolean) node.get("isEntity");
                        break;
                    }
                }

                // If it's an enum, mark as skipped and continue to next file
                if (isEnum) {
                    result.put("success", true);
                    result.put("skipped", true);
                    result.put("reason", "enum");
                    results.add(result);
                    continue;
                }

                // Second pass: collect all symbols (class + methods)
                for (Map<String, Object> node : analyzer.getNodes()) {
                    String nodeType = (String) node.get("nodeType");
                    String fqn = (String) node.get("fqn");

                    Map<String, Object> symbol = new HashMap<>();
                    symbol.put("fqn", fqn);
                    symbol.put("nodeType", nodeType);
                    symbol.put("line", node.get("lineNumber"));
                    symbol.put("isEntity", isEntity);
                    symbols.add(symbol);
                }

                // Build result for this file
                result.put("success", true);
                result.put("class_fqn", classFqn);
                result.put("is_entity", isEntity);
                result.put("symbols", symbols);
                results.add(result);

            } catch (Exception e) {
                result.put("success", false);
                result.put("error", "Failed to index " + classFilePath + ": " + e.getMessage());
                results.add(result);
            }
        }

        // Build batch response
        Map<String, Object> response = new HashMap<>();
        response.put("success", true);
        response.put("results", results);

        return mapper.writeValueAsString(response);
    }
}
