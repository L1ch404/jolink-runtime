package net.jolink.runtime.jdt;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.StringReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.UUID;

import org.eclipse.core.resources.IMarker;
import org.eclipse.core.runtime.NullProgressMonitor;
import org.eclipse.jdt.core.IImportDeclaration;
import org.eclipse.jdt.core.IJavaProject;
import org.eclipse.jdt.core.IType;
import org.eclipse.jdt.core.Flags;
import org.eclipse.jdt.core.Signature;
import org.eclipse.jdt.core.dom.AST;
import org.eclipse.jdt.core.dom.ASTNode;
import org.eclipse.jdt.core.dom.ASTParser;
import org.eclipse.jdt.core.dom.ASTVisitor;
import org.eclipse.jdt.core.dom.AbstractTypeDeclaration;
import org.eclipse.jdt.core.dom.AnonymousClassDeclaration;
import org.eclipse.jdt.core.dom.LambdaExpression;
import org.eclipse.jdt.core.dom.ThisExpression;
import org.eclipse.jdt.core.eval.ICodeSnippetRequestor;
import org.eclipse.jdt.core.eval.IEvaluationContext;

import static net.jolink.runtime.jdt.WorkerApplication.*;

/** Compile debugger snippets without writing to the project's build outputs. */
final class SnippetCompiler implements ICodeSnippetRequestor {
    private final Map<String, byte[]> classes = new LinkedHashMap<>();
    private final List<String> diagnostics = new ArrayList<>();
    private String snippetClass;
    private String localVarPrefix;
    private int errors;

    static String compile(List<IJavaProject> projects, String encoded) throws Exception {
        Properties request = new Properties();
        request.load(new StringReader(new String(Base64.getDecoder().decode(encoded), StandardCharsets.UTF_8)));
        String declaringName = value(request, "declaring_type");
        IJavaProject project = projects.get(0);
        IType declaringType = null;
        for (IJavaProject candidate : projects) {
            IType found = candidate.findType(declaringName.replace('$', '.'));
            if (found != null) {
                project = candidate;
                declaringType = found;
                // Prefer the module defining this type over a dependent module.
                if (found.getJavaProject().equals(candidate)) break;
            }
        }
        if (declaringType == null) {
            return "{\"ok\":false,\"error_code\":\"EVALUATION_CONTEXT_UNAVAILABLE\","
                    + "\"diagnostics\":[" + json("Cannot resolve suspended type " + declaringName) + "]}";
        }
        IEvaluationContext context = project.newEvaluationContext();
        context.setPackageName(declaringType.getPackageFragment().getElementName());
        Set<String> imports = new LinkedHashSet<>();
        if (declaringType.getCompilationUnit() != null) {
            for (IImportDeclaration declaration : declaringType.getCompilationUnit().getImports()) {
                imports.add((Flags.isStatic(declaration.getFlags()) ? "static " : "") + declaration.getElementName());
            }
        }
        int importCount = Integer.parseInt(request.getProperty("import_count", "0"));
        for (int i = 0; i < importCount; i++) imports.add(value(request, "import." + i));
        context.setImports(imports.toArray(new String[0]));
        int count = Integer.parseInt(request.getProperty("local_count", "0"));
        // JDT's suspended-local bridge accepts erased types. Give it private
        // aliases, then declare the actual (possibly generic) locals in Java.
        // They must be real locals: a closure can outlive this evaluation.
        String aliasPrefix = "__jolink_" + UUID.randomUUID().toString().replace("-", "") + "_";
        String[] types = new String[count], aliases = new String[count];
        StringBuilder declarations = new StringBuilder(), writeback = new StringBuilder();
        for (int i = 0; i < count; i++) {
            String signature = value(request, "local_type." + i).replace('/', '.');
            String name = value(request, "local_name." + i);
            types[i] = Signature.toString(Signature.getTypeErasure(signature));
            aliases[i] = aliasPrefix + name;
            declarations.append(Signature.toString(signature)).append(' ').append(name)
                    .append(" = ").append(aliases[i]).append(";\n");
            writeback.append(aliases[i]).append(" = ").append(name).append(";\n");
        }
        String expression = value(request, "expression").strip();
        if (expression.endsWith(";")) expression = expression.substring(0, expression.length() - 1);
        String receiver = aliasPrefix + "this";
        String bound = bindLambdaReceiver(expression, receiver, project);
        if (!bound.equals(expression)) {
            declarations.append(declaringType.getFullyQualifiedName('.')).append(' ')
                    .append(receiver).append(" = this;\n");
        }
        String snippet = declarations + "try {\nreturn (" + bound + "\n);\n} finally {\n"
                + writeback + "}\n";
        SnippetCompiler result = new SnippetCompiler();
        result.localVarPrefix = LOCAL_VAR_PREFIX + aliasPrefix;
        context.evaluateCodeSnippet(snippet, types, aliases, new int[count],
                declaringType, Boolean.parseBoolean(request.getProperty("is_static", "false")),
                Boolean.parseBoolean(request.getProperty("is_constructor", "false")),
                result, new NullProgressMonitor());
        return result.resultJson();
    }

    /** Core snippets rewrite top-level this, but not Java's lexical lambda this. */
    private static String bindLambdaReceiver(String expression, String receiver, IJavaProject project) {
        ASTParser parser = ASTParser.newParser(AST.getJLSLatest());
        parser.setKind(ASTParser.K_EXPRESSION);
        parser.setCompilerOptions(project.getOptions(true));
        parser.setSource(expression.toCharArray());
        ASTNode root = parser.createAST(new NullProgressMonitor());
        ASTNode boundary = root.getParent(); // DOM fragments may have synthetic enclosing types.
        List<ThisExpression> references = new ArrayList<>();
        root.accept(new ASTVisitor() {
            @Override
            public boolean visit(ThisExpression node) {
                if (node.getQualifier() != null) return false;
                boolean lambda = false;
                for (ASTNode parent = node.getParent(); parent != null && parent != boundary; parent = parent.getParent()) {
                    // An anonymous class has its own this, also in its lambdas.
                    if (parent instanceof AnonymousClassDeclaration || parent instanceof AbstractTypeDeclaration) return false;
                    if (parent instanceof LambdaExpression) lambda = true;
                }
                if (lambda) references.add(node);
                return false;
            }
        });
        StringBuilder result = new StringBuilder(expression);
        for (int i = references.size() - 1; i >= 0; i--) {
            ThisExpression node = references.get(i);
            result.replace(node.getStartPosition(), node.getStartPosition() + node.getLength(), receiver);
        }
        return result.toString();
    }

    private static String value(Properties values, String key) {
        return new String(Base64.getDecoder().decode(values.getProperty(key, "")), StandardCharsets.UTF_8);
    }

    @Override
    public boolean acceptClassFiles(byte[][] bytes, String[][] names, String main) {
        for (int i = 0; i < bytes.length; i++) classes.put(String.join(".", names[i]), bytes[i]);
        if (main != null) snippetClass = main;
        return true;
    }

    @Override
    public void acceptProblem(IMarker problem, String fragment, int kind) {
        if (problem.getAttribute(IMarker.SEVERITY, -1) == IMarker.SEVERITY_ERROR) {
            errors++;
            diagnostics.add(problem.getAttribute(IMarker.MESSAGE, "Snippet compilation failed."));
        }
    }

    private String resultJson() throws Exception {
        boolean ok = errors == 0 && snippetClass != null;
        List<String> output = new ArrayList<>();
        // JDT's snippet counter restarts with the Worker. Give each compilation
        // its own names so an existing target ClassLoader can load it safely.
        String prefix = "JoLink_" + UUID.randomUUID().toString().replace("-", "") + "_";
        Map<String, String> renamed = new LinkedHashMap<>();
        for (String name : classes.keySet()) {
            if (!name.equals("org.eclipse.jdt.internal.eval.target.CodeSnippet")) {
                int start = name.lastIndexOf('.') + 1;
                renamed.put(name, name.substring(0, start) + prefix + name.substring(start));
            }
        }
        String unique = renamed.get(snippetClass);
        if (ok) for (Map.Entry<String, byte[]> entry : classes.entrySet()) {
            String name = renamed.getOrDefault(entry.getKey(), entry.getKey());
            byte[] bytes = rename(entry.getValue(), renamed);
            output.add("{\"name\":" + json(name) + ",\"bytes_base64\":"
                    + json(Base64.getEncoder().encodeToString(bytes)) + "}");
        }
        return "{\"ok\":" + ok + ",\"error_count\":" + errors
                + ",\"error_code\":" + (ok ? "null" : json("EVALUATION_COMPILE_FAILED"))
                + ",\"diagnostics\":" + jsonArray(diagnostics)
                + ",\"snippet_class\":" + (unique == null ? "null" : json(unique))
                + ",\"classes\":" + jsonObjectsArray(output)
                + ",\"local_var_prefix\":" + json(localVarPrefix)
                + ",\"delegate_this\":" + json(DELEGATE_THIS)
                + ",\"run_method\":" + json(RUN_METHOD)
                + ",\"result_value_field\":" + json(RESULT_VALUE_FIELD)
                + ",\"result_type_field\":" + json(RESULT_TYPE_FIELD) + "}";
    }

    /** Rename generated class references, including descriptors and inner classes. */
    private static byte[] rename(byte[] bytes, Map<String, String> renamed) throws Exception {
        DataInputStream input = new DataInputStream(new ByteArrayInputStream(bytes));
        int magic = input.readInt(), version = input.readInt();
        int count = input.readUnsignedShort();
        List<byte[]> entries = new ArrayList<>();
        List<String> text = new ArrayList<>();
        entries.add(null); text.add(null);
        for (int i = 1; i < count; i++) {
            int tag = input.readUnsignedByte();
            if (tag == 1) {
                String value = input.readUTF();
                entries.add(utf8(renameReference(value, renamed))); text.add(value);
            } else {
                int size = switch (tag) {
                    case 3, 4, 9, 10, 11, 12, 17, 18 -> 4;
                    case 5, 6 -> 8;
                    case 7, 8, 16, 19, 20 -> 2;
                    case 15 -> 3;
                    default -> throw new IllegalArgumentException("Unknown class constant " + tag);
                };
                byte[] entry = new byte[size + 1]; entry[0] = (byte) tag;
                input.readFully(entry, 1, size);
                entries.add(entry); text.add(null);
                if (tag == 5 || tag == 6) { i++; entries.add(null); text.add(null); }
            }
        }
        // A string literal can share a UTF8 constant with a generated class
        // name. Preserve literal bytes instead of changing the user's result.
        for (int i = 1; i < count; i++) {
            byte[] entry = entries.get(i);
            if (entry != null && entry[0] == 8) {
                int index = (entry[1] & 255) * 256 + (entry[2] & 255);
                String original = text.get(index);
                if (!original.equals(renameReference(original, renamed))) {
                    int preserved = entries.size();
                    entries.add(utf8(original));
                    entry[1] = (byte) (preserved >>> 8); entry[2] = (byte) preserved;
                }
            }
        }
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        DataOutputStream output = new DataOutputStream(buffer);
        output.writeInt(magic); output.writeInt(version); output.writeShort(entries.size());
        for (byte[] entry : entries) if (entry != null) output.write(entry);
        input.transferTo(output);
        return buffer.toByteArray();
    }

    private static byte[] utf8(String value) throws Exception {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        DataOutputStream output = new DataOutputStream(buffer);
        output.writeByte(1); output.writeUTF(value);
        return buffer.toByteArray();
    }

    private static String renameReference(String value, Map<String, String> names) {
        for (Map.Entry<String, String> replacement : names.entrySet()) {
            String oldName = replacement.getKey(), newName = replacement.getValue();
            if (value.equals(oldName)) return newName;
            String oldInternal = oldName.replace('.', '/'), newInternal = newName.replace('.', '/');
            if (value.equals(oldInternal)) return newInternal;
        }
        for (Map.Entry<String, String> replacement : names.entrySet()) {
            String oldInternal = replacement.getKey().replace('.', '/');
            String newInternal = replacement.getValue().replace('.', '/');
            value = value.replace("L" + oldInternal + ";", "L" + newInternal + ";")
                    .replace("L" + oldInternal + "<", "L" + newInternal + "<");
        }
        return value;
    }
}
