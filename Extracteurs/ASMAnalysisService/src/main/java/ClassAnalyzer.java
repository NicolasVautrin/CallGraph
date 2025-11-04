import org.objectweb.asm.*;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.*;

/**
 * Analyzes a single .class file using ASM
 */
public class ClassAnalyzer extends ClassVisitor {
    private static final Logger logger = LoggerFactory.getLogger(ClassAnalyzer.class);

    private final Path classFile;
    private final List<Map<String, Object>> nodes = new ArrayList<>();
    private final List<Map<String, Object>> edges = new ArrayList<>();

    private String className;
    private boolean isInterface;
    private boolean isEnum;
    private boolean isAbstract;
    private List<String> modifiers;

    public ClassAnalyzer(Path classFile) {
        super(Opcodes.ASM9);
        this.classFile = classFile;
    }

    public void analyze() throws IOException {
        byte[] classBytes = Files.readAllBytes(classFile);
        ClassReader reader = new ClassReader(classBytes);
        reader.accept(this, ClassReader.EXPAND_FRAMES);
    }

    @Override
    public void visit(int version, int access, String name, String signature,
                      String superName, String[] interfaces) {
        // Convert internal name to FQN: com/example/MyClass -> com.example.MyClass
        this.className = name.replace('/', '.');

        this.isInterface = (access & Opcodes.ACC_INTERFACE) != 0;
        this.isEnum = (access & Opcodes.ACC_ENUM) != 0;
        this.isAbstract = (access & Opcodes.ACC_ABSTRACT) != 0;
        this.modifiers = parseModifiers(access);

        // Detect if this class is an Axelor entity (extends AuditableModel)
        boolean isEntity = false;
        if (superName != null) {
            String superFqn = superName.replace('/', '.');
            // Check if extends AuditableModel (direct or via package convention)
            isEntity = superFqn.contains("AuditableModel") ||
                       (className.contains(".db.") && !className.equals("com.axelor.db.Model"));
        }

        // Create class node
        Map<String, Object> classNode = new HashMap<>();
        classNode.put("fqn", className);
        classNode.put("nodeType", isInterface ? "interface" : (isEnum ? "enum" : "class"));
        classNode.put("modifiers", modifiers);
        classNode.put("isInterface", isInterface);
        classNode.put("isEnum", isEnum);
        classNode.put("isAbstract", isAbstract);
        classNode.put("isEntity", isEntity);
        nodes.add(classNode);

        // Inheritance edges
        if (superName != null && !superName.equals("java/lang/Object")) {
            String superFqn = superName.replace('/', '.');
            Map<String, Object> edge = new HashMap<>();
            edge.put("edgeType", "inheritance");
            edge.put("fromFqn", className);
            edge.put("toFqn", superFqn);
            edge.put("kind", "extends");
            edges.add(edge);
        }

        // Interface implementations
        if (interfaces != null) {
            for (String iface : interfaces) {
                String ifaceFqn = iface.replace('/', '.');
                Map<String, Object> edge = new HashMap<>();
                edge.put("edgeType", "inheritance");
                edge.put("fromFqn", className);
                edge.put("toFqn", ifaceFqn);
                edge.put("kind", "implements");
                edges.add(edge);
            }
        }

        super.visit(version, access, name, signature, superName, interfaces);
    }

    @Override
    public FieldVisitor visitField(int access, String name, String descriptor,
                                    String signature, Object value) {
        // Extract field type from descriptor
        String fieldType = descriptorToClassName(descriptor);

        if (fieldType != null && !isPrimitive(fieldType)) {
            // member_of edge: FieldType -> Class (kind=class)
            Map<String, Object> edge = new HashMap<>();
            edge.put("edgeType", "member_of");
            edge.put("fromFqn", fieldType);
            edge.put("toFqn", className);
            edge.put("kind", "class");
            edges.add(edge);
        }

        return super.visitField(access, name, descriptor, signature, value);
    }

    @Override
    public MethodVisitor visitMethod(int access, String name, String descriptor,
                                      String signature, String[] exceptions) {
        String methodFqn = className + "." + name + descriptorToSignature(descriptor);

        // Extract method modifiers (visibility, static, final, etc.)
        List<String> methodModifiers = parseModifiers(access);

        // Parse method signature for types (return and arguments)
        Type methodType = Type.getMethodType(descriptor);

        // Return type
        Type returnType = methodType.getReturnType();
        if (returnType.getSort() == Type.OBJECT || returnType.getSort() == Type.ARRAY) {
            String returnFqn = returnType.getClassName();
            if (!isPrimitive(returnFqn)) {
                Map<String, Object> edge = new HashMap<>();
                edge.put("edgeType", "member_of");
                edge.put("fromFqn", returnFqn);
                edge.put("toFqn", methodFqn);
                edge.put("kind", "return");
                edges.add(edge);
            }
        }

        // Argument types
        Type[] argumentTypes = methodType.getArgumentTypes();
        logger.info("[ARGS] Method {} has {} arguments", methodFqn, argumentTypes.length);
        for (int i = 0; i < argumentTypes.length; i++) {
            Type argType = argumentTypes[i];
            logger.info("[ARGS]   Arg {}: sort={}, className={}", i, argType.getSort(),
                       (argType.getSort() == Type.OBJECT || argType.getSort() == Type.ARRAY) ? argType.getClassName() : "N/A");

            if (argType.getSort() == Type.OBJECT || argType.getSort() == Type.ARRAY) {
                String argFqn = argType.getClassName();
                boolean primitive = isPrimitive(argFqn);
                logger.info("[ARGS]   -> argFqn={}, isPrimitive={}", argFqn, primitive);

                if (!primitive) {
                    Map<String, Object> edge = new HashMap<>();
                    edge.put("edgeType", "member_of");
                    edge.put("fromFqn", argFqn);
                    edge.put("toFqn", methodFqn);
                    edge.put("kind", "argument");
                    edges.add(edge);
                    logger.info("[ARGS]   -> ADDED argument edge: {} -> {}", argFqn, methodFqn);
                } else {
                    logger.info("[ARGS]   -> SKIPPED (primitive)");
                }
            }
        }

        // Return MethodAnalyzer to track method body, line numbers, and annotations
        return new MethodAnalyzer(methodFqn, methodModifiers);
    }

    /**
     * Method visitor to extract method calls with line numbers
     */
    private class MethodAnalyzer extends MethodVisitor {
        private final String currentMethodFqn;
        private int currentLine = -1;
        private int methodStartLine = -1;
        private Map<String, Object> methodNode;

        public MethodAnalyzer(String currentMethodFqn, List<String> modifiers) {
            super(Opcodes.ASM9);
            this.currentMethodFqn = currentMethodFqn;

            // Create method node immediately to capture ALL methods
            // (including those without line number info like some setters)
            methodNode = new HashMap<>();
            methodNode.put("fqn", currentMethodFqn);
            methodNode.put("nodeType", "method");
            methodNode.put("lineNumber", -1);  // Will be updated if line info available
            methodNode.put("modifiers", modifiers);
            methodNode.put("hasOverride", false);  // Will be updated if @Override found
            methodNode.put("isTransactional", false);  // Will be updated if @Transactional found
            nodes.add(methodNode);
            logger.info("[METHOD_NODE_CREATED] {} (total nodes: {})", currentMethodFqn, nodes.size());

            // member_of edge: Method -> Class (kind=method)
            Map<String, Object> memberEdge = new HashMap<>();
            memberEdge.put("edgeType", "member_of");
            memberEdge.put("fromFqn", currentMethodFqn);
            memberEdge.put("toFqn", className);
            memberEdge.put("kind", "method");
            edges.add(memberEdge);
        }

        @Override
        public AnnotationVisitor visitAnnotation(String descriptor, boolean visible) {
            // Detect @Override annotation
            if ("Ljava/lang/Override;".equals(descriptor)) {
                methodNode.put("hasOverride", true);
                logger.info("[ANNOTATION] @Override detected on {}", currentMethodFqn);
            }

            // Detect @Transactional annotation (Spring or Jakarta)
            if ("Lorg/springframework/transaction/annotation/Transactional;".equals(descriptor) ||
                "Ljavax/transaction/Transactional;".equals(descriptor) ||
                "Ljakarta/transaction/Transactional;".equals(descriptor)) {
                methodNode.put("isTransactional", true);
                logger.info("[ANNOTATION] @Transactional detected on {}", currentMethodFqn);
            }

            return super.visitAnnotation(descriptor, visible);
        }

        @Override
        public void visitLineNumber(int line, Label start) {
            this.currentLine = line;
            if (this.methodStartLine == -1) {
                this.methodStartLine = line;
                // Update the existing method node with the real line number
                methodNode.put("lineNumber", line);
            }
            super.visitLineNumber(line, start);
        }

        @Override
        public void visitMethodInsn(int opcode, String owner, String name,
                                     String descriptor, boolean isInterface) {
            String targetClass = owner.replace('/', '.');
            String targetMethod = targetClass + "." + name + descriptorToSignature(descriptor);

            String kind = (opcode == Opcodes.INVOKESPECIAL && name.equals("<init>")) ?
                "new" : "standard";

            logger.info("[CALL] {} -> {} (kind: {}, line: {})", currentMethodFqn, targetMethod, kind, currentLine);

            Map<String, Object> edge = new HashMap<>();
            edge.put("edgeType", "call");
            edge.put("fromFqn", currentMethodFqn);
            edge.put("toFqn", targetMethod);
            edge.put("kind", kind);
            edge.put("lineNumber", currentLine);  // Line where the call is made
            edges.add(edge);

            logger.debug("  Edge added to edges list (size now: {})", edges.size());

            super.visitMethodInsn(opcode, owner, name, descriptor, isInterface);
        }

        public int getMethodStartLine() {
            return methodStartLine;
        }
    }

    // Helper methods

    private List<String> parseModifiers(int access) {
        List<String> mods = new ArrayList<>();
        if ((access & Opcodes.ACC_PUBLIC) != 0) mods.add("public");
        if ((access & Opcodes.ACC_PRIVATE) != 0) mods.add("private");
        if ((access & Opcodes.ACC_PROTECTED) != 0) mods.add("protected");
        if ((access & Opcodes.ACC_STATIC) != 0) mods.add("static");
        if ((access & Opcodes.ACC_FINAL) != 0) mods.add("final");
        if ((access & Opcodes.ACC_ABSTRACT) != 0) mods.add("abstract");
        return mods;
    }

    private String descriptorToClassName(String descriptor) {
        Type type = Type.getType(descriptor);
        if (type.getSort() == Type.OBJECT || type.getSort() == Type.ARRAY) {
            return type.getClassName();
        }
        return null;
    }

    private String descriptorToSignature(String descriptor) {
        Type methodType = Type.getMethodType(descriptor);
        StringBuilder sig = new StringBuilder("(");

        Type[] args = methodType.getArgumentTypes();
        for (int i = 0; i < args.length; i++) {
            if (i > 0) sig.append(", ");
            sig.append(args[i].getClassName());
        }
        sig.append(")");

        return sig.toString();
    }

    private boolean isPrimitive(String className) {
        return className.equals("void") ||
               className.equals("boolean") ||
               className.equals("byte") ||
               className.equals("char") ||
               className.equals("short") ||
               className.equals("int") ||
               className.equals("long") ||
               className.equals("float") ||
               className.equals("double") ||
               className.startsWith("java.lang.") ||
               className.equals("java.lang.String") ||
               className.equals("java.lang.Object");
    }

    public List<Map<String, Object>> getNodes() {
        return nodes;
    }

    public List<Map<String, Object>> getEdges() {
        return edges;
    }
}
