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
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Let Maven filter/copy resources; keep their outputs separate from old javac classes. */
final class ResourcePreparation implements AutoCloseable {
    static final String MAIN = "jolink.resources.main";
    static final String TEST = "jolink.resources.test";
    private final Map<Path, Output> outputs = new LinkedHashMap<>();

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
