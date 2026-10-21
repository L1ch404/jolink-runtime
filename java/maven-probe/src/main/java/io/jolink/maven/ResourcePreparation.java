package io.jolink.maven;

import java.io.File;
import java.io.DataInputStream;
import java.io.EOFException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.plugin.BuildPluginManager;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.plugin.PluginParameterExpressionEvaluator;
import org.apache.maven.model.Resource;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Let Maven filter/copy resources; keep their outputs separate from old javac classes. */
final class ResourcePreparation implements AutoCloseable {
    static final String MAIN = "jolink.resources.main";
    static final String TEST = "jolink.resources.test";
    static final String INPUTS = "jolink.resources.inputs";
    private final Map<Path, Output> outputs = new LinkedHashMap<>();
    private final List<Resource> resourceSets = new ArrayList<>();

    static boolean handles(MojoExecution execution) {
        return "org.apache.maven.plugins".equals(execution.getGroupId())
            && "maven-resources-plugin".equals(execution.getArtifactId())
            && Arrays.asList("resources", "testResources", "copy-resources").contains(execution.getGoal());
    }

    void execute(MavenSession session, MavenProject project, MojoExecution execution,
            BuildPluginManager plugins) throws Exception {
        Xpp3Dom original = execution.getConfiguration();
        Xpp3Dom configured = new Xpp3Dom(original);
        Xpp3Dom output = configured.getChild("outputDirectory");
        String expression = output.getValue();
        if (expression == null) expression = output.getAttribute("default-value");
        PluginParameterExpressionEvaluator evaluator = new PluginParameterExpressionEvaluator(session, execution);
        Object resolved = evaluator.evaluate(expression);
        Path destination = evaluator.alignToBaseDirectory(resolved instanceof File ? (File) resolved
            : new File(String.valueOf(resolved))).getCanonicalFile().toPath();
        Output prepared = outputs.get(destination);
        if (prepared == null) {
            byte[] hash = MessageDigest.getInstance("SHA-256").digest(
                (project.getBasedir().getCanonicalPath() + "\n" + destination).getBytes(StandardCharsets.UTF_8));
            StringBuilder key = new StringBuilder();
            for (byte b : hash) key.append(String.format("%02x", b & 255));
            Path root = Paths.get(System.getProperty("jolink.probe.resourceCache",
                System.getProperty("user.home") + "/.cache/jolink-runtime/maven-resources"));
            prepared = new Output(destination, root.resolve(key.toString()));
            outputs.put(destination, prepared);
        }
        output.setValue(prepared.staging.toString());
        try {
            execution.setConfiguration(configured);
            plugins.executeMojo(session, execution);
            collectInputs(original, evaluator, execution);
            // A later preparation goal may read the normal Maven output path.
            prepared.publish(false);
        } finally {
            execution.setConfiguration(original);
        }
    }

    void finish(MavenProject project, Set<String> inputs) throws Exception {
        for (Output output : outputs.values()) {
            output.publish(true);
            inputs.add(output.directory.toString());
            for (Path file : files(output.directory)) inputs.add(output.nativeDirectory.resolve(file).toString());
        }
        project.setContextValue(MAIN, directories(project.getBuild().getOutputDirectory()));
        project.setContextValue(TEST, directories(project.getBuild().getTestOutputDirectory()));
        project.setContextValue(INPUTS, resourceSets);
    }

    private void collectInputs(Xpp3Dom configuration, PluginParameterExpressionEvaluator evaluator,
            MojoExecution execution) throws Exception {
        if (Boolean.parseBoolean(String.valueOf(value(configuration.getChild("skip"), evaluator)))) return;
        Xpp3Dom configured = configuration.getChild("resources");
        if (configured == null) return;
        List<Resource> resources = new ArrayList<>();
        if (configured.getChildCount() == 0) {
            Object resolved = value(configured, evaluator);
            if (resolved instanceof Collection<?>)
                for (Object item : (Collection<?>) resolved) resources.add((Resource) item);
        } else {
            for (Xpp3Dom child : configured.getChildren()) {
                Resource resource = new Resource();
                resource.setDirectory(String.valueOf(value(child.getChild("directory"), evaluator)));
                resource.setIncludes(patterns(child.getChild("includes"), evaluator));
                resource.setExcludes(patterns(child.getChild("excludes"), evaluator));
                resources.add(resource);
            }
        }
        boolean defaults = !"false".equalsIgnoreCase(String.valueOf(value(configuration.getChild("addDefaultExcludes"), evaluator)));
        // Read the resource plugin's scanner defaults, not a different Plexus
        // version from the Probe's compile-time dependencies/Maven API realm.
        String[] defaultExcludes = defaults ? (String[]) execution.getMojoDescriptor().getPluginDescriptor()
            .getClassRealm().loadClass("org.codehaus.plexus.util.AbstractScanner")
            .getField("DEFAULTEXCLUDES").get(null) : new String[0];
        for (Resource resource : resources) {
            Resource input = new Resource();
            input.setDirectory(evaluator.alignToBaseDirectory(new File(resource.getDirectory())).getCanonicalPath());
            input.setIncludes(new ArrayList<>(resource.getIncludes()));
            input.setExcludes(new ArrayList<>(resource.getExcludes()));
            for (String pattern : defaultExcludes) input.addExclude(pattern);
            resourceSets.add(input);
        }
    }

    private static Object value(Xpp3Dom node, PluginParameterExpressionEvaluator evaluator) throws Exception {
        if (node == null) return null;
        String value = node.getValue() == null ? node.getAttribute("default-value") : node.getValue();
        return value == null ? null : evaluator.evaluate(value);
    }

    private static List<String> patterns(Xpp3Dom node, PluginParameterExpressionEvaluator evaluator) throws Exception {
        List<String> result = new ArrayList<>();
        if (node != null) for (Xpp3Dom child : node.getChildren()) result.add(String.valueOf(value(child, evaluator)));
        return result;
    }

    private List<String> directories(String nativePath) throws Exception {
        Output output = outputs.get(new File(nativePath).getCanonicalFile().toPath());
        return output == null ? Collections.emptyList() : Collections.singletonList(output.directory.toString());
    }

    private static Set<Path> files(Path root) throws Exception {
        if (!Files.isDirectory(root)) return Collections.emptySet();
        try (Stream<Path> paths = Files.walk(root)) {
            return paths.filter(Files::isRegularFile).map(root::relativize).collect(Collectors.toCollection(LinkedHashSet::new));
        }
    }

    @Override
    public void close() throws Exception {
        for (Output output : outputs.values()) {
            try (Stream<Path> paths = Files.walk(output.staging)) {
                for (Path path : paths.sorted(Comparator.reverseOrder()).collect(Collectors.toList())) Files.delete(path);
            }
        }
    }

    private static final class Output {
        final Path nativeDirectory, directory, ownership, staging;
        final Set<Path> previous;
        Output(Path destination, Path directory) throws Exception {
            this.nativeDirectory = destination;
            this.directory = directory;
            ownership = directory.resolveSibling(directory.getFileName() + ".files");
            previous = new LinkedHashSet<>();
            if (Files.isRegularFile(ownership))
                for (String name : Files.readAllLines(ownership, StandardCharsets.UTF_8)) previous.add(Paths.get(name));
            Files.createDirectories(directory);
            // Give Maven a fresh output set without deleting resources already
            // used by another cached launch/Test model.
            staging = Files.createTempDirectory(directory.getParent(), "resources-");
        }
        void publish(boolean removeObsolete) throws Exception {
            Set<Path> current = files(staging);
            for (Path relative : current) {
                copyChanged(staging.resolve(relative), directory.resolve(relative));
                copyChanged(staging.resolve(relative), nativeDirectory.resolve(relative));
            }
            if (removeObsolete) {
                for (Path relative : files(directory)) if (!current.contains(relative)) Files.delete(directory.resolve(relative));
                for (Path relative : previous) if (!current.contains(relative)) Files.deleteIfExists(nativeDirectory.resolve(relative));
                Files.write(ownership, current.stream().map(Path::toString).collect(Collectors.toList()), StandardCharsets.UTF_8);
            }
        }
        private static void copyChanged(Path source, Path destination) throws Exception {
            if (sameContent(source, destination)) return;
            Files.createDirectories(destination.getParent());
            Files.copy(source, destination, StandardCopyOption.REPLACE_EXISTING);
        }
        private static boolean sameContent(Path source, Path destination) throws Exception {
            if (!Files.isRegularFile(destination) || Files.size(source) != Files.size(destination)) return false;
            try (InputStream left = Files.newInputStream(source);
                    DataInputStream right = new DataInputStream(Files.newInputStream(destination))) {
                byte[] a = new byte[8192], b = new byte[8192];
                for (int count; (count = left.read(a)) != -1;) {
                    right.readFully(b, 0, count);
                    for (int i = 0; i < count; i++) if (a[i] != b[i]) return false;
                }
                return right.read() == -1;
            } catch (EOFException shortened) {
                return false;
            }
        }
    }
}
