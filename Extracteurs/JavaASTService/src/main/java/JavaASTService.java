import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;
import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;
import com.github.javaparser.ParserConfiguration;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.JsonNode;

import java.io.File;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.LinkedHashMap;
import java.util.Set;
import java.util.HashSet;
import java.util.Arrays;

/**
 * Java AST Service - Extracts usages from Java files using JavaParser
 *
 * FEATURES:
 *   - Type resolution with JavaSymbolSolver (FQN detection)
 *   - Parser caching: reuses TypeSolver for same repo combinations
 *   - Supports multiple repositories for cross-project resolution
 *
 * USAGE:
 *   Start: java JavaASTService [port]
 *   Default port: 8765
 *
 * API ENDPOINTS:
 *
 *   POST /analyze
 *   Description: Analyze Java files and extract usages
 *   Request Body:
 *     {
 *       "files": ["path1.java", "path2.java", ...],
 *       "repos": ["repo1/path", "repo2/path", ...]  // Optional
 *     }
 *   Response:
 *     {
 *       "processed": N,
 *       "failed": M,
 *       "elapsed_ms": T,
 *       "results": [...]
 *     }
 *   Notes:
 *     - If "repos" is provided, creates/reuses a TypeSolver for those repos
 *     - If "repos" is omitted, uses global parser from config.json
 *     - Parser cache: same repo combination = reused TypeSolver (faster)
 *
 *   GET /health
 *   Description: Health check endpoint
 *   Response: {"status": "ok", "service": "JavaASTService"}
 *
 *   POST /shutdown
 *   Description: Gracefully shutdown the service
 *   Response: {"status": "shutting down"}
 *
 * PARSER CACHING:
 *   The service maintains a cache of JavaParser instances indexed by repository sets.
 *   When analyzing files with repos=[A, B, C]:
 *     - First call: creates TypeSolver for A, B, C and caches it
 *     - Subsequent calls with same repos: reuses cached parser (fast)
 *     - Different repo set: creates new parser and caches it
 *   Benefits:
 *     - Avoids recreating TypeSolver (expensive operation)
 *     - Optimal type resolution with correct repo context
 *     - Memory efficient: same repos = same parser
 */
public class JavaASTService {

    private static JavaParser parser;
    private static HttpServer server;
    private static final ObjectMapper mapper = new ObjectMapper();
    private static List<String> domainPatterns = new ArrayList<>();

    // Cache of parsers by repository set (for efficient type resolution)
    private static final Map<Set<String>, JavaParser> parserCache = new LinkedHashMap<>();

    public static void main(String[] args) throws IOException {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8765;

        System.out.println("=== JavaAST Service ===");
        System.out.println("Initializing type resolver...");
        setupTypeResolver();
        System.out.println("Type resolver ready!");
        System.out.println("Domain patterns: " + domainPatterns);

        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/analyze", new AnalyzeHandler());
        server.createContext("/health", new HealthHandler());
        server.createContext("/shutdown", new ShutdownHandler());
        server.setExecutor(null); // Default executor

        System.out.println("Server listening on port " + port);
        System.out.println("Endpoints:");
        System.out.println("  POST /analyze - Analyze Java files");
        System.out.println("  GET  /health  - Health check");
        System.out.println("  POST /shutdown - Shutdown server");
        server.start();
    }

    private static void setupTypeResolver() throws IOException {
        CombinedTypeSolver typeSolver = new CombinedTypeSolver();

        // JDK types
        typeSolver.add(new ReflectionTypeSolver());

        // Load configuration
        JsonNode config = loadConfig();

        // Load domain patterns
        JsonNode patterns = config.get("domain_patterns");
        if (patterns != null && patterns.isArray()) {
            for (int i = 0; i < patterns.size(); i++) {
                domainPatterns.add(patterns.get(i).asText());
            }
            System.out.println("Loaded " + domainPatterns.size() + " domain pattern(s)");
        } else {
            // Default pattern if not configured
            domainPatterns.add("com.axelor.*");
            System.out.println("No domain_patterns in config, using default: com.axelor.*");
        }

        // Load repositories and scan for src/main/java and build/src-gen/java directories
        JsonNode repositories = config.get("repositories");
        if (repositories != null && repositories.isArray()) {
            System.out.println("Scanning repositories for Java sources...");
            List<File> sourceDirs = new ArrayList<>();

            for (int i = 0; i < repositories.size(); i++) {
                String repoPath = repositories.get(i).asText();
                File repoDir = new File(repoPath);

                if (!repoDir.exists() || !repoDir.isDirectory()) {
                    System.out.println("  Repository not found (skipped): " + repoPath);
                    continue;
                }

                System.out.println("  Scanning: " + repoDir.getAbsolutePath());
                findJavaSourceDirs(repoDir, sourceDirs);
            }

            System.out.println("Found " + sourceDirs.size() + " Java source directories");
            System.out.println();

            // Add all found source directories to type solver
            for (File sourceDir : sourceDirs) {
                typeSolver.add(new com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver(sourceDir));
                System.out.println("  Added: " + sourceDir.getAbsolutePath());
            }
        }

        JavaSymbolSolver symbolSolver = new JavaSymbolSolver(typeSolver);
        ParserConfiguration parserConfig = new ParserConfiguration()
                .setSymbolResolver(symbolSolver);
        parser = new JavaParser(parserConfig);
    }

    private static JsonNode loadConfig() throws IOException {
        String configPath = "config.json";
        File configFile = new File(configPath);

        if (!configFile.exists()) {
            System.out.println("WARNING: config.json not found, using empty configuration");
            return mapper.createObjectNode();
        }

        System.out.println("Loading configuration from: " + configFile.getAbsolutePath());
        String content = new String(Files.readAllBytes(Paths.get(configPath)), StandardCharsets.UTF_8);
        return mapper.readTree(content);
    }

    /**
     * Recursively find all src/main/java and build/src-gen/java directories in a repository
     */
    private static void findJavaSourceDirs(File dir, List<File> result) {
        String path = dir.getAbsolutePath().replace('\\', '/');

        // Check if this is a Java source directory
        if (path.endsWith("/src/main/java") || path.endsWith("/build/src-gen/java")) {
            result.add(dir);
            return; // Don't recurse into Java source directories
        }

        // Recurse into subdirectories
        File[] children = dir.listFiles();
        if (children != null) {
            for (File child : children) {
                if (child.isDirectory() && !child.getName().startsWith(".")) {
                    findJavaSourceDirs(child, result);
                }
            }
        }
    }

    /**
     * Get or create a JavaParser for a specific set of repositories
     * Uses cache to avoid recreating parsers for the same repository sets
     */
    private static JavaParser getOrCreateParser(List<String> repoPaths) throws IOException {
        // Normalize paths (absolute, forward slashes)
        Set<String> normalizedRepos = new HashSet<>();
        for (String repoPath : repoPaths) {
            File repoFile = new File(repoPath);
            String normalized = repoFile.getAbsolutePath().replace('\\', '/');
            normalizedRepos.add(normalized);
        }

        // Check cache
        if (parserCache.containsKey(normalizedRepos)) {
            System.out.println("[CACHE] Using cached parser for " + normalizedRepos.size() + " repos");
            return parserCache.get(normalizedRepos);
        }

        // Create new parser
        System.out.println("[NEW] Creating parser for " + normalizedRepos.size() + " repos:");
        for (String repo : normalizedRepos) {
            System.out.println("  - " + repo);
        }

        CombinedTypeSolver typeSolver = new CombinedTypeSolver();
        typeSolver.add(new ReflectionTypeSolver());

        // Scan repositories for Java source directories
        List<File> sourceDirs = new ArrayList<>();
        for (String repoPath : normalizedRepos) {
            File repoDir = new File(repoPath);
            if (!repoDir.exists() || !repoDir.isDirectory()) {
                System.out.println("  [WARN] Repository not found (skipped): " + repoPath);
                continue;
            }
            findJavaSourceDirs(repoDir, sourceDirs);
        }

        System.out.println("  Found " + sourceDirs.size() + " Java source directories");

        // Add all found source directories to type solver
        for (File sourceDir : sourceDirs) {
            typeSolver.add(new com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver(sourceDir));
        }

        // Create parser
        JavaSymbolSolver symbolSolver = new JavaSymbolSolver(typeSolver);
        ParserConfiguration parserConfig = new ParserConfiguration()
                .setSymbolResolver(symbolSolver);
        JavaParser newParser = new JavaParser(parserConfig);

        // Cache it
        parserCache.put(normalizedRepos, newParser);
        System.out.println("  OK - Parser cached");

        return newParser;
    }

    static class AnalyzeHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (!"POST".equals(exchange.getRequestMethod())) {
                sendResponse(exchange, 405, "{\"error\": \"Method not allowed\"}");
                return;
            }

            try {
                String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
                JsonNode request = mapper.readTree(body);
                JsonNode files = request.get("files");

                // Check if repos are specified
                JavaParser parserToUse = parser; // Default to global parser
                JsonNode reposNode = request.get("repos");
                if (reposNode != null && reposNode.isArray()) {
                    List<String> repos = new ArrayList<>();
                    for (int i = 0; i < reposNode.size(); i++) {
                        repos.add(reposNode.get(i).asText());
                    }
                    parserToUse = getOrCreateParser(repos);
                }

                long startTime = System.currentTimeMillis();

                List<Map<String, Object>> results = new ArrayList<>();
                int processed = 0;
                int failed = 0;

                for (int i = 0; i < files.size(); i++) {
                    String filePath = files.get(i).asText();
                    File javaFile = new File(filePath);

                    try {
                        // Validate file exists
                        if (!javaFile.exists()) {
                            throw new IOException("File not found: " + filePath);
                        }
                        if (!javaFile.isFile()) {
                            throw new IOException("Path is not a file: " + filePath);
                        }
                        if (!javaFile.canRead()) {
                            throw new IOException("Cannot read file: " + filePath);
                        }

                        Map<String, Object> result = extractFromFile(javaFile, parserToUse);
                        results.add(result);

                        if ((Boolean) result.getOrDefault("success", false)) {
                            processed++;
                        } else {
                            failed++;
                        }
                    } catch (Exception e) {
                        System.err.println("ERROR processing file: " + filePath);
                        System.err.println("  Error: " + e.getClass().getSimpleName() + " - " + e.getMessage());
                        e.printStackTrace();

                        Map<String, Object> errorResult = new LinkedHashMap<>();
                        errorResult.put("success", false);
                        errorResult.put("file", javaFile.getAbsolutePath());

                        List<String> errors = new ArrayList<>();
                        errors.add(e.getClass().getSimpleName() + ": " + e.getMessage());
                        errorResult.put("errors", errors);
                        errorResult.put("error_type", e.getClass().getSimpleName());

                        // Add stack trace
                        List<String> stackTrace = new ArrayList<>();
                        for (StackTraceElement element : e.getStackTrace()) {
                            stackTrace.add(element.toString());
                        }
                        errorResult.put("stack_trace", stackTrace);

                        results.add(errorResult);
                        failed++;
                    }
                }

                long elapsed = System.currentTimeMillis() - startTime;

                Map<String, Object> output = new LinkedHashMap<>();
                output.put("processed", processed);
                output.put("failed", failed);
                output.put("elapsed_ms", elapsed);
                output.put("results", results);

                sendResponse(exchange, 200, mapper.writerWithDefaultPrettyPrinter().writeValueAsString(output));

            } catch (Exception e) {
                System.err.println("FATAL ERROR in analyze handler:");
                e.printStackTrace();

                Map<String, Object> error = new LinkedHashMap<>();
                error.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
                error.put("error_type", e.getClass().getSimpleName());

                // Add stack trace
                List<String> stackTrace = new ArrayList<>();
                for (StackTraceElement element : e.getStackTrace()) {
                    stackTrace.add(element.toString());
                }
                error.put("stack_trace", stackTrace);

                sendResponse(exchange, 500, mapper.writerWithDefaultPrettyPrinter().writeValueAsString(error));
            }
        }
    }

    static class HealthHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            Map<String, Object> response = new LinkedHashMap<>();
            response.put("status", "ok");
            response.put("service", "JavaASTService");
            sendResponse(exchange, 200, mapper.writeValueAsString(response));
        }
    }

    static class ShutdownHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            Map<String, Object> response = new LinkedHashMap<>();
            response.put("status", "shutting down");
            sendResponse(exchange, 200, mapper.writeValueAsString(response));

            // Shutdown in separate thread
            new Thread(() -> {
                try {
                    Thread.sleep(500);
                    System.out.println("Shutting down...");
                    server.stop(0);
                    System.exit(0);
                } catch (InterruptedException e) {
                    e.printStackTrace();
                }
            }).start();
        }
    }

    private static void sendResponse(HttpExchange exchange, int statusCode, String response) throws IOException {
        byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().add("Content-Type", "application/json");
        exchange.sendResponseHeaders(statusCode, bytes.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(bytes);
        }
    }

    private static Map<String, Object> extractFromFile(File javaFile, JavaParser parserToUse) throws IOException {
        ParseResult<CompilationUnit> result = parserToUse.parse(javaFile);

        if (!result.isSuccessful()) {
            Map<String, Object> errorResult = new LinkedHashMap<>();
            errorResult.put("success", false);
            errorResult.put("file", javaFile.getAbsolutePath());
            List<String> errors = new ArrayList<>();
            result.getProblems().forEach(p -> errors.add(p.getMessage()));
            errorResult.put("errors", errors);
            return errorResult;
        }

        CompilationUnit cu = result.getResult().get();

        Map<String, Object> output = new LinkedHashMap<>();
        output.put("success", true);
        output.put("file", javaFile.getAbsolutePath());

        // Extract package
        String packageName = cu.getPackageDeclaration()
            .map(pkg -> pkg.getNameAsString())
            .orElse("");
        output.put("package", packageName);

        // Extract module from file path
        String module = extractModule(javaFile.getAbsolutePath());
        output.put("module", module);

        // Collect usages with full context
        UsageCollector usageCollector = new UsageCollector(packageName, module, javaFile.getAbsolutePath(), domainPatterns);
        cu.accept(usageCollector, null);

        output.put("usages", usageCollector.usages);
        output.put("usage_count", usageCollector.usages.size());

        return output;
    }

    private static String extractModule(String filePath) {
        // Extract module from path: modules/xxx/src/main/java/...
        // Example: C:\...\modules\open-auction-base\src\... => open-auction-base
        String normalized = filePath.replace('\\', '/');

        int modulesIdx = normalized.indexOf("/modules/");
        if (modulesIdx == -1) {
            return "unknown";
        }

        String afterModules = normalized.substring(modulesIdx + 9); // "/modules/".length()
        int nextSlash = afterModules.indexOf('/');
        if (nextSlash == -1) {
            return "unknown";
        }

        return afterModules.substring(0, nextSlash);
    }
}
