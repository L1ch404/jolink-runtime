package net.jolink.runtime.jdt;

import java.lang.module.ModuleFinder;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.eclipse.core.resources.IProject;
import org.eclipse.jdt.core.IClasspathAttribute;
import org.eclipse.jdt.core.JavaCore;
import org.eclipse.jdt.core.dom.AST;
import org.eclipse.jdt.core.dom.ASTParser;
import org.eclipse.jdt.core.dom.CompilationUnit;
import org.eclipse.jdt.core.dom.ModuleDeclaration;

/** Translate resolved module inputs into the Java Builder's native attributes. */
final class JavaModuleSupport {
    private List<String> modulePath = List.of();
    private Set<String> modularEntries = Set.of();
    private String patchModule;
    private List<String> reads = List.of();
    private List<String> exports = List.of();
    private boolean moduleMode;

    void configure(String modulePathFile, String patchModule, String readsFile, String exportsFile) throws Exception {
        modulePath = lines(modulePathFile);
        modularEntries = new LinkedHashSet<>();
        for (String entry : modulePath) modularEntries.add(key(entry));
        this.patchModule = patchModule == null || patchModule.isEmpty() ? null : patchModule;
        reads = lines(readsFile);
        exports = lines(exportsFile);
    }

    void prepare(IProject project) {
        moduleMode = !modulePath.isEmpty() || patchModule != null || !reads.isEmpty() || !exports.isEmpty()
                || Files.isRegularFile(project.getLocation().toFile().toPath().resolve("src/module-info.java"));
    }

    IClasspathAttribute[] attributes(String entry, IProject project, IClasspathAttribute... existing) throws Exception {
        List<IClasspathAttribute> result = new ArrayList<>(List.of(existing));
        boolean platform = !entry.startsWith("project:") && Paths.get(entry).getFileName().toString().equals("jrt-fs.jar");
        if ((platform && moduleMode) || modularEntries.contains(key(entry))) {
            result.add(JavaCore.newClasspathAttribute(IClasspathAttribute.MODULE, "true"));
            if (patchModule != null && !platform && patchModule.equals(moduleName(entry, project))) {
                result.add(JavaCore.newClasspathAttribute(IClasspathAttribute.PATCH_MODULE,
                        patchModule + "=" + project.getFullPath().append("src").toPortableString()));
            }
        }
        // JDT's ModuleUpdater collects these globally from classpath attributes.
        // Put them on the one system entry, avoiding duplicate update edges.
        if (platform) {
            if (!reads.isEmpty()) result.add(JavaCore.newClasspathAttribute(IClasspathAttribute.ADD_READS, String.join(":", reads)));
            if (!exports.isEmpty()) result.add(JavaCore.newClasspathAttribute(IClasspathAttribute.ADD_EXPORTS, String.join(":", exports)));
        }
        return result.toArray(new IClasspathAttribute[0]);
    }

    String metadata(IProject project) throws Exception {
        return "{\"module_name\":" + WorkerApplication.jsonNullable(sourceModuleName(project))
                + ",\"patched_module_name\":" + WorkerApplication.jsonNullable(patchModule)
                + ",\"module_path\":" + WorkerApplication.jsonArray(modulePath) + "}";
    }

    private static String moduleName(String entry, IProject project) throws Exception {
        if (entry.startsWith("project:")) {
            return sourceModuleName(project.getWorkspace().getRoot().getProject(entry.substring(8)));
        }
        return ModuleFinder.of(Paths.get(entry)).findAll().stream()
                .map(reference -> reference.descriptor().name()).findFirst().orElse(null);
    }

    static String sourceModuleName(IProject project) throws Exception {
        Path descriptor = project.getLocation().toFile().toPath().resolve("src/module-info.java");
        if (!Files.isRegularFile(descriptor)) return null;
        Charset encoding = Charset.forName(project.getFolder("src").getDefaultCharset(true));
        ASTParser parser = ASTParser.newParser(AST.getJLSLatest());
        parser.setKind(ASTParser.K_COMPILATION_UNIT);
        parser.setUnitName("module-info.java");
        Map<String, String> options = new LinkedHashMap<>();
        JavaCore.setComplianceOptions(JavaCore.VERSION_9, options);
        parser.setCompilerOptions(options);
        parser.setSource(Files.readString(descriptor, encoding).toCharArray());
        ModuleDeclaration module = ((CompilationUnit) parser.createAST(null)).getModule();
        return module == null ? null : module.getName().getFullyQualifiedName();
    }

    private static List<String> lines(String file) throws Exception {
        if (file == null || file.isEmpty()) return List.of();
        return Files.readAllLines(Paths.get(file), StandardCharsets.UTF_8).stream()
                .map(String::trim).filter(value -> !value.isEmpty()).toList();
    }

    private static String key(String entry) {
        return entry.startsWith("project:") ? entry : Paths.get(entry).toAbsolutePath().normalize().toString();
    }
}
