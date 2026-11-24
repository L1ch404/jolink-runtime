package io.jolink.gradle;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.*;
import org.gradle.api.GradleException;
import org.gradle.api.Project;
import org.gradle.api.Task;
import org.gradle.api.internal.GradleInternal;
import org.gradle.api.invocation.Gradle;
import org.gradle.api.initialization.ProjectDescriptor;
import org.gradle.api.tasks.SourceSet;
import org.gradle.api.tasks.SourceSetContainer;
import org.gradle.configuration.ApplyScriptPluginBuildOperationType;
import org.gradle.internal.operations.BuildOperationDescriptor;
import org.gradle.internal.operations.BuildOperationListener;
import org.gradle.internal.operations.BuildOperationListenerManager;
import org.gradle.internal.operations.OperationFinishEvent;
import org.gradle.internal.operations.OperationIdentifier;
import org.gradle.internal.operations.OperationProgressEvent;
import org.gradle.internal.operations.OperationStartEvent;

/** Capture evaluated builds, including buildSrc and arbitrarily named included builds. */
final class GradleConfigurationInputs {
    /** First observation belongs to the model; later observations cannot replace it. */
    static final class ObservedInputs {
        final Set<File> scripts = new LinkedHashSet<>();
        final Map<String, String> stamps = new LinkedHashMap<>();
        final Set<String> changes = new LinkedHashSet<>();

        synchronized void record(File file) {
            try {
                Path path = file.getCanonicalFile().toPath();
                if (Files.isDirectory(path)) {
                    observe(path.toString(), "directory");
                    // A build under gradle/ has inputs AND generated state. Keep
                    // its configs and sources, not Gradle's cache and outputs.
                    boolean gradleDirectory = path.endsWith("gradle");
                    Files.walkFileTree(path, new SimpleFileVisitor<Path>() {
                        @Override public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) {
                            if (gradleDirectory && (dir.endsWith(".gradle") || dir.endsWith("build"))) {
                                for (String script : Arrays.asList("settings.gradle", "settings.gradle.kts",
                                        "build.gradle", "build.gradle.kts")) {
                                    if (Files.isRegularFile(dir.resolveSibling(script))) return FileVisitResult.SKIP_SUBTREE;
                                }
                            }
                            return FileVisitResult.CONTINUE;
                        }
                        @Override public FileVisitResult visitFile(Path child, BasicFileAttributes attrs) {
                            if (Files.isRegularFile(child)) record(child.toFile());
                            return FileVisitResult.CONTINUE;
                        }
                    });
                } else {
                    observe(path.toString(), Files.isRegularFile(path) ? sha256(Files.readAllBytes(path)) : "missing");
                }
            } catch (IOException error) {
                throw new GradleException("Unable to capture Gradle configuration input: " + file, error);
            }
        }

        private void observe(String path, String value) {
            String previous = stamps.putIfAbsent(path, value);
            if (previous != null && !previous.equals(value)) changes.add(path);
        }

        synchronized void script(File file) {
            scripts.add(file);
            record(file);
        }

        private static String sha256(byte[] bytes) {
            try {
                byte[] digest = MessageDigest.getInstance("SHA-256").digest(bytes);
                StringBuilder result = new StringBuilder(64);
                for (byte value : digest) result.append(String.format(Locale.ROOT, "%02x", value & 255));
                return result.toString();
            } catch (NoSuchAlgorithmException impossible) {
                throw new IllegalStateException(impossible);
            }
        }
    }

    static ObservedInputs observeScripts(Gradle gradle, Path output) {
        ObservedInputs observed = new ObservedInputs();
        Path probeInit = output.getParent().resolve("gradle-init.private.gradle").toAbsolutePath().normalize();
        BuildOperationListenerManager operations = ((GradleInternal) gradle).getServices()
                .get(BuildOperationListenerManager.class);
        BuildOperationListener listener = new BuildOperationListener() {
            @Override
            public void started(BuildOperationDescriptor operation, OperationStartEvent event) {
                Object details = operation.getDetails();
                if (!(details instanceof ApplyScriptPluginBuildOperationType.Details)) return;
                String path = ((ApplyScriptPluginBuildOperationType.Details) details).getFile();
                if (path == null) return; // Remote script freshness is outside local file tracking.
                File file = new File(path);
                // The operation wraps ScriptPlugin.apply(), where normal local
                // scripts are compiled/evaluated. Keep this early observation,
                // not a hash taken when the export task eventually executes.
                if (!file.toPath().toAbsolutePath().normalize().equals(probeInit)) observed.script(file);
            }

            @Override
            public void progress(OperationIdentifier id, OperationProgressEvent event) {}

            @Override
            public void finished(BuildOperationDescriptor operation, OperationFinishEvent event) {}
        };
        operations.addListener(listener);
        gradle.buildFinished(result -> operations.removeListener(listener));
        gradle.beforeSettings(settings -> {
            File root = settings.getSettingsDir();
            for (String name : Arrays.asList("settings.gradle", "settings.gradle.kts", "gradle.properties", "gradle"))
                observed.record(new File(root, name));
        });
        // Project properties are loaded before beforeProject callbacks. Settings
        // has already resolved custom project directories, but that load has not
        // happened yet at settingsEvaluated.
        gradle.settingsEvaluated(settings -> recordProjects(settings.getRootProject(), observed));
        gradle.beforeProject(project -> {
            observed.record(project.getBuildFile());
            observed.record(new File(project.getProjectDir(), "gradle.properties"));
        });
        return observed;
    }

    private static void recordProjects(ProjectDescriptor project, ObservedInputs observed) {
        observed.record(new File(project.getProjectDir(), project.getBuildFileName()));
        observed.record(new File(project.getProjectDir(), "gradle.properties"));
        for (ProjectDescriptor child : project.getChildren()) recordProjects(child, observed);
    }

    static void capture(Gradle gradle, Path output, ObservedInputs observed) {
        try {
            Project root = gradle.getRootProject();
            Properties facts = new Properties();
            facts.setProperty("root", root.getProjectDir().getCanonicalPath());
            List<File> files = new ArrayList<>();
            List<File> directories = new ArrayList<>();
            List<File> sources = new ArrayList<>();
            Map<Task, Set<File>> sourceProducers = new LinkedHashMap<>();
            boolean nestedBuild = ((GradleInternal) gradle).getIdentityPath().getParent() != null;
            files.add(new File(root.getProjectDir(), "settings.gradle"));
            files.add(new File(root.getProjectDir(), "settings.gradle.kts"));
            files.add(new File(root.getProjectDir(), "gradle.properties"));
            directories.add(new File(root.getProjectDir(), "gradle"));
            for (Project project : root.getAllprojects()) {
                files.add(project.getBuildFile());
                files.add(new File(project.getProjectDir(), "gradle.properties"));
                SourceSetContainer sets = project.getExtensions().findByType(SourceSetContainer.class);
                if (sets != null) for (SourceSet set : sets) {
                    Set<File> roots = new LinkedHashSet<>();
                    for (File source : set.getAllSource().getSrcDirs()) {
                        if (!source.toPath().startsWith(project.getBuildDir().toPath())) {
                            sources.add(source);
                            roots.add(source);
                        }
                    }
                    if (nestedBuild) for (Task producer : set.getOutput().getClassesDirs().getBuildDependencies().getDependencies(null))
                        sourceProducers.computeIfAbsent(producer, ignored -> new LinkedHashSet<>()).addAll(roots);
                }
            }
            add(facts, "file.", files);
            add(facts, "directory.", directories);
            add(facts, "source.", sources);
            for (File file : files) observed.record(file);
            for (File directory : directories) observed.record(directory);
            Path destination = output.getParent().resolve("gradle-configuration-inputs");
            Files.createDirectories(destination);
            String name = UUID.nameUUIDFromBytes(facts.getProperty("root").getBytes(StandardCharsets.UTF_8)).toString();
            Path record = destination.resolve(name + ".properties");
            save(record, facts, observed);
            // Another included build's configuration can generate these sources
            // after this project's evaluation. Capture at the actual output
            // producer boundary, after configuration, before it compiles.
            gradle.getTaskGraph().beforeTask(task -> {
                Set<File> roots = sourceProducers.get(task);
                if (roots == null) return;
                synchronized (observed) {
                    for (File rootDir : roots) {
                        observed.record(rootDir);
                        try { facts.setProperty("consumedSource." + rootDir.getCanonicalPath(), "true"); }
                        catch (IOException error) { throw new GradleException("Unable to record build logic source root", error); }
                    }
                    save(record, facts, observed);
                }
            });
        } catch (IOException error) {
            throw new GradleException("Unable to record Gradle configuration inputs", error);
        }
    }

    private static void save(Path record, Properties facts, ObservedInputs observed) {
        synchronized (observed) {
            for (Map.Entry<String, String> entry : observed.stamps.entrySet())
                facts.setProperty("stamp." + entry.getKey(), entry.getValue());
            for (String path : observed.changes) facts.setProperty("changed." + path, "true");
            try (Writer writer = Files.newBufferedWriter(record, StandardCharsets.UTF_8)) {
                facts.store(writer, "Gradle evaluated build inputs");
            } catch (IOException error) {
                throw new GradleException("Unable to record Gradle configuration inputs", error);
            }
        }
    }

    static Map<String, Object> collect(Project primary, Path output, ObservedInputs observed) throws IOException {
        Set<String> files = new LinkedHashSet<>();
        Set<String> directories = new LinkedHashSet<>();
        Set<String> sourceDirectories = new LinkedHashSet<>();
        Map<String, String> stamps = new LinkedHashMap<>();
        Set<String> changes = new LinkedHashSet<>();
        synchronized (observed) {
            for (File script : observed.scripts) files.add(script.getCanonicalPath());
            stamps.putAll(observed.stamps);
            changes.addAll(observed.changes);
        }
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(
                output.getParent().resolve("gradle-configuration-inputs"), "*.properties")) {
            for (Path path : stream) {
                Properties facts = new Properties();
                try (Reader reader = Files.newBufferedReader(path, StandardCharsets.UTF_8)) { facts.load(reader); }
                boolean buildLogic = !primary.getProjectDir().getCanonicalPath().equals(facts.getProperty("root"));
                for (String key : facts.stringPropertyNames()) {
                    if (key.startsWith("file.")) files.add(facts.getProperty(key));
                    if (key.startsWith("directory.") || (buildLogic && key.startsWith("source."))) {
                        directories.add(facts.getProperty(key));
                    }
                    if (buildLogic && key.startsWith("consumedSource.")) sourceDirectories.add(key.substring("consumedSource.".length()));
                    if (key.startsWith("stamp.")) {
                        String file = key.substring("stamp.".length()), value = facts.getProperty(key);
                        String previous = stamps.putIfAbsent(file, value);
                        if (previous != null && !previous.equals(value)) changes.add(file);
                    }
                    if (key.startsWith("changed.")) changes.add(key.substring("changed.".length()));
                }
            }
        }
        Map<String, Object> result = new LinkedHashMap<>();
        List<String> orderedFiles = new ArrayList<>(files);
        List<String> orderedDirectories = new ArrayList<>(directories);
        Collections.sort(orderedFiles);
        Collections.sort(orderedDirectories);
        result.put("configurationFiles", orderedFiles);
        result.put("configurationDirectories", orderedDirectories);
        List<String> orderedSources = new ArrayList<>(sourceDirectories);
        Collections.sort(orderedSources);
        result.put("configurationSourceDirectories", orderedSources);
        result.put("configurationStamps", stamps);
        result.put("configurationChanges", new ArrayList<>(changes));
        return result;
    }

    private static void add(Properties facts, String prefix, List<File> files) throws IOException {
        int index = 0;
        for (File file : files) facts.setProperty(prefix + index++, file.getCanonicalPath());
    }
}
