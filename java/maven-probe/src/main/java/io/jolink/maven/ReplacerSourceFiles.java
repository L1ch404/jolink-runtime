package io.jolink.maven;

import java.io.File;
import java.nio.file.*;
import java.util.*;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.plugin.*;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Ask Replacer itself which files it would read and write, before writing any. */
final class ReplacerSourceFiles {
    private static final String NS = "com.google.code.maven_replacer_plugin.";
    private static final Set<String> ROUTING = new HashSet<>(Arrays.asList(
            "file", "outputFile", "includes", "excludes", "filesToInclude", "filesToExclude",
            "outputDir", "outputBasedir", "inputFilePattern", "outputFilePattern"));

    @SuppressWarnings("unchecked")
    static void execute(MavenSession session, MavenProject project, MojoExecution e, BuildPluginManager plugins,
                        Map<String,String> overrides) throws Exception {
        MavenPluginManager manager = session.getContainer().lookup(MavenPluginManager.class);
        // As in BuildPluginManager.executeMojo: older Maven does not create the
        // plugin realm inside getConfiguredMojo and cannot look up its role.
        plugins.getPluginRealm(session, e.getMojoDescriptor().getPluginDescriptor());
        Mojo instance = manager.getConfiguredMojo(Mojo.class, session, e);
        List<Path[]> pairs = new ArrayList<>();
        boolean bulk;
        try {
            ClassLoader realm = instance.getClass().getClassLoader();
            Class<?> type = realm.loadClass(NS + "ReplacerMojo");
            if ((Boolean) type.getMethod("isSkip").invoke(instance)) return;
            String base = (String) type.getMethod("getBasedir").invoke(instance);
            String single = (String) type.getMethod("getFile").invoke(instance);
            // An optional input (including a previous private generator's output)
            // can disable the whole bulk execution, not just one replacement.
            if (single != null) {
                File input = new File(single);
                if (!input.isAbsolute() && base != null) input = new File(base, single);
                String prepared = overrides.get(input.getCanonicalPath());
                if (prepared != null) type.getMethod("setFile", String.class).invoke(instance, prepared);
            }
            java.lang.reflect.Method missing = type.getDeclaredMethod("checkFileExists");
            missing.setAccessible(true);
            if ((Boolean) missing.invoke(instance)) return;
            if (single != null) type.getMethod("setFile", String.class).invoke(instance, single);
            List<String> includes = patterns(type, instance, "getIncludes", "getFilesToInclude");
            List<String> excludes = patterns(type, instance, "getExcludes", "getFilesToExclude");
            List<String> files;
            bulk = !includes.isEmpty();
            if (bulk) {
                Class<?> selector = realm.loadClass(NS + "include.FileSelector");
                Object scanner = selector.getConstructor().newInstance();
                java.lang.reflect.Method select = selector.getMethod("listIncludes", String.class, List.class, List.class);
                files = new ArrayList<>((List<String>) select.invoke(scanner, base, includes, excludes));
                // A previous generator can have produced Java that exists only in
                // our private view. Let the same native scanner select those names.
                if (base != null && !overrides.isEmpty()) {
                    Path root = new File(base).getCanonicalFile().toPath();
                    List<Path> absent = new ArrayList<>();
                    for (String name : overrides.keySet()) {
                        Path path = Paths.get(name);
                        if (path.startsWith(root) && !Files.exists(path)) absent.add(root.relativize(path));
                    }
                    if (!absent.isEmpty()) {
                        Path tree = Files.createTempDirectory("jolink-replacer-selection-");
                        try {
                            for (Path rel : absent) {
                                Files.createDirectories(tree.resolve(rel).getParent()); Files.createFile(tree.resolve(rel));
                            }
                            files.addAll((List<String>) select.invoke(scanner, tree.toString(), includes, excludes));
                            Collections.sort(files); // Ant DirectoryScanner#getIncludedFiles returns sorted names.
                        } finally {
                            try (java.util.stream.Stream<Path> paths = Files.walk(tree)) {
                                for (Path p : paths.sorted(Comparator.reverseOrder()).toArray(Path[]::new)) Files.delete(p);
                            }
                        }
                    }
                }
                java.lang.reflect.Method limit = type.getDeclaredMethod("limit", List.class);
                limit.setAccessible(true);
                files = (List<String>) limit.invoke(instance, files);
            } else files = single == null ? Collections.emptyList() : Collections.singletonList(single);
            Class<?> builder = realm.loadClass(NS + "OutputFilenameBuilder");
            Object filenames = builder.getConstructor().newInstance();
            java.lang.reflect.Method output = builder.getMethod("buildFrom", String.class, type);
            for (String raw : files) {
                File input = new File(raw);
                if (!input.isAbsolute() && base != null && !base.trim().isEmpty()) input = new File(base, raw);
                Path in = input.toPath().toAbsolutePath().normalize(), key = PreparedSourceFile.sourceKey(project, in);
                pairs.add(new Path[]{key == null ? in : key,
                    new File((String) output.invoke(filenames, raw, instance)).toPath().toAbsolutePath().normalize()});
            }
        } finally { manager.releaseMojo(instance, e); }

        boolean privateIO = false;
        for (Path[] pair : pairs)
            if (overrides.containsKey(pair[0].toString()) || PreparedSourceFile.sourceKey(project, pair[1]) != null) privateIO = true;
        if (!privateIO) { plugins.executeMojo(session, e); return; }
        // Keep native token/regex/delimiter/encoding behavior. The original
        // outputDir/pattern must not override the chosen private destination.
        for (Path[] pair : pairs) {
            Xpp3Dom config = new Xpp3Dom(e.getConfiguration());
            for (int i=config.getChildCount()-1; i>=0; i--) {
                String key = config.getChild(i).getName();
                // In bulk mode the native limit applies to files, not replacements per file.
                if (ROUTING.contains(key) || (bulk && "maxReplacements".equals(key))) config.removeChild(i);
            }
            parameter(config, "file", overrides.getOrDefault(pair[0].toString(), pair[0].toString()));
            parameter(config, "outputFile", pair[1].toString());
            PreparedSourceFile.run(session, project, e, plugins, config, pair[1], overrides);
        }
    }

    @SuppressWarnings("unchecked")
    private static List<String> patterns(Class<?> type, Object instance, String list, String csv) throws Exception {
        List<String> result = new ArrayList<>((List<String>) type.getMethod(list).invoke(instance));
        String more = (String) type.getMethod(csv).invoke(instance);
        if (more != null) for (String entry : more.split(",")) result.add(entry.trim());
        return result;
    }

    private static void parameter(Xpp3Dom config, String name, String value) {
        Xpp3Dom node = new Xpp3Dom(name); node.setValue(value); config.addChild(node);
    }
}
