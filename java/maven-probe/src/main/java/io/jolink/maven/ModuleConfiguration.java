package io.jolink.maven;

import java.io.File;
import java.util.*;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.plugin.BuildPluginManager;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Native compiler-plugin descriptor parsing and binary path classification. */
final class ModuleConfiguration {
    static void append(StringBuilder out, MavenSession session, MavenProject project,
            CompilerExecutions executions, BuildPluginManager plugins) throws Exception {
        File main = selectedDescriptor(project.getCompileSourceRoots(), executions.main.configuration, "includes", "excludes");
        boolean tests = Boolean.parseBoolean(project.getProperties().getProperty("jolink.probe.testSourcesRequired", "true"));
        File test = tests ? selectedDescriptor(project.getTestCompileSourceRoots(), executions.test.configuration, "testIncludes", "testExcludes") : null;
        out.append(",\"jpms\":{");
        scope(out, "main", session, project, executions.main, plugins, main, null, project.getCompileClasspathElements());
        out.append(',');
        boolean useModules = tests && !"false".equalsIgnoreCase(value(executions.test.configuration, "useModulePath"));
        scope(out, "test", session, project, executions.test, plugins, useModules ? test : null,
                useModules && test == null ? main : null, project.getTestClasspathElements());
        out.append('}');
    }

    @SuppressWarnings("unchecked")
    private static void scope(StringBuilder out, String label, MavenSession session, MavenProject project,
            CompilerExecutions.Scope scope, BuildPluginManager plugins, File descriptor, File patch,
            List<String> classpath) throws Exception {
        out.append('"').append(label).append("\":{");
        File active = descriptor == null ? patch : descriptor;
        String name = null;
        List<String> modulepath = new ArrayList<>();
        if (active != null && scope.execution != null) {
            ClassLoader loader = plugins.getPluginRealm(session, scope.execution.getMojoDescriptor().getPluginDescriptor());
            Class<?> managerType = loader.loadClass("org.codehaus.plexus.languages.java.jpms.LocationManager");
            Class<?> requestType = loader.loadClass("org.codehaus.plexus.languages.java.jpms.ResolvePathsRequest");
            Object manager = managerType.getConstructor().newInstance();
            Object mainDescriptor = parsed(manager, active);
            name = (String) mainDescriptor.getClass().getMethod("name").invoke(mainDescriptor);
            Map<String, File> projectOutputs = new LinkedHashMap<>();
            Map<String, Object> projectDescriptors = new LinkedHashMap<>();
            Set<String> allOutputs = new HashSet<>();
            Set<String> classpathKeys = new HashSet<>();
            for (String path : classpath) classpathKeys.add(new File(path).getCanonicalPath());
            for (MavenProject reactor : session.getProjects()) {
                File output = new File(reactor.getBuild().getOutputDirectory()).getCanonicalFile();
                allOutputs.add(output.getPath());
                File source = descriptor(reactor.getCompileSourceRoots());
                if (source != null && classpathKeys.contains(output.getPath())) {
                    Object info = parsed(manager, source);
                    String module = (String) info.getClass().getMethod("name").invoke(info);
                    projectOutputs.put(module, output);
                    projectDescriptors.put(module, info);
                }
            }
            // Clean reactor outputs do not yet contain module-info.class. Walk
            // native-parsed source requires and let Plexus resolve binary graphs.
            Set<String> required = new LinkedHashSet<>();
            Deque<String> pending = new ArrayDeque<>();
            pending.add(name);
            projectDescriptors.put(name, mainDescriptor);
            while (!pending.isEmpty()) {
                String module = pending.removeFirst();
                if (!required.add(module)) continue;
                Object info = projectDescriptors.get(module);
                if (info != null) for (Object requires : (Set<?>) info.getClass().getMethod("requires").invoke(info)) {
                    pending.add((String) requires.getClass().getMethod("name").invoke(requires));
                }
            }
            List<File> binaries = new ArrayList<>();
            for (String path : classpath) if (!allOutputs.contains(new File(path).getCanonicalPath())) binaries.add(new File(path));
            Object request = requestType.getMethod("ofFiles", Collection.class).invoke(null, binaries);
            requestType.getMethod("setMainModuleDescriptor", Object.class).invoke(request, active);
            requestType.getMethod("setIncludeStatic", boolean.class).invoke(request, true);
            requestType.getMethod("setAdditionalModules", Collection.class).invoke(request, required);
            Object result = managerType.getMethod("resolvePaths", requestType).invoke(manager, request);
            Set<File> selected = ((Map<File, ?>) result.getClass().getMethod("getModulepathElements").invoke(result)).keySet();
            Set<String> selectedPaths = new HashSet<>();
            for (File file : selected) selectedPaths.add(file.getCanonicalPath());
            for (String module : required) if (projectOutputs.containsKey(module)) selectedPaths.add(projectOutputs.get(module).getPath());
            for (String path : classpath) if (selectedPaths.contains(new File(path).getCanonicalPath())) modulepath.add(new File(path).getCanonicalPath());
        }
        scope.moduleName = descriptor == null ? null : name;
        ExportBuildWorldMojo.field(out, "moduleName", scope.moduleName, false);
        ExportBuildWorldMojo.field(out, "moduleDescriptor", descriptor == null ? null : descriptor.getCanonicalPath(), true);
        ExportBuildWorldMojo.field(out, "patchModule", patch == null ? null : name, true);
        ExportBuildWorldMojo.stringList(out, "modulePath", modulepath, true);
        ExportBuildWorldMojo.stringList(out, "addReads", patch == null ? Collections.emptyList() : Collections.singletonList(name + "=ALL-UNNAMED"), true);
        out.append('}');
    }

    private static Object parsed(Object manager, File descriptor) throws Exception {
        Object parsed = manager.getClass().getMethod("parseModuleDescriptor", File.class).invoke(manager, descriptor);
        return parsed.getClass().getMethod("getModuleDescriptor").invoke(parsed);
    }

    static File descriptor(List<String> roots) throws Exception {
        for (String root : roots) {
            File file = new File(root, "module-info.java");
            if (file.isFile()) return file.getCanonicalFile();
        }
        return null;
    }

    private static File selectedDescriptor(List<String> roots, Xpp3Dom configuration, String includes, String excludes) throws Exception {
        Xpp3Dom included = configuration.getChild(includes), excluded = configuration.getChild(excludes);
        boolean selected = included == null || included.getChildCount() == 0;
        if (included != null) for (Xpp3Dom pattern : included.getChildren())
            selected |= org.codehaus.plexus.util.SelectorUtils.matchPath(pattern.getValue(), "module-info.java");
        if (excluded != null) for (Xpp3Dom pattern : excluded.getChildren())
            if (org.codehaus.plexus.util.SelectorUtils.matchPath(pattern.getValue(), "module-info.java")) selected = false;
        return selected ? descriptor(roots) : null;
    }

    private static String value(Xpp3Dom configuration, String name) {
        Xpp3Dom child = configuration.getChild(name);
        return child == null || child.getValue() == null ? "" : child.getValue().trim();
    }
}
