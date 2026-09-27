package io.jolink.gradle;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import org.gradle.api.GradleException;
import org.gradle.api.Project;
import org.gradle.api.internal.GradleInternal;
import org.gradle.api.invocation.Gradle;
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
    static Set<File> observeScripts(Gradle gradle, Path output) {
        Set<File> scripts = Collections.synchronizedSet(new LinkedHashSet<>());
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
                if (!file.toPath().toAbsolutePath().normalize().equals(probeInit)) scripts.add(file);
            }

            @Override
            public void progress(OperationIdentifier id, OperationProgressEvent event) {}

            @Override
            public void finished(BuildOperationDescriptor operation, OperationFinishEvent event) {}
        };
        operations.addListener(listener);
        gradle.buildFinished(result -> operations.removeListener(listener));
        return scripts;
    }

    static void capture(Gradle gradle, Path output) {
        try {
            Project root = gradle.getRootProject();
            Properties facts = new Properties();
            facts.setProperty("root", root.getProjectDir().getCanonicalPath());
            List<File> files = new ArrayList<>();
            List<File> directories = new ArrayList<>();
            List<File> sources = new ArrayList<>();
            files.add(new File(root.getProjectDir(), "settings.gradle"));
            files.add(new File(root.getProjectDir(), "settings.gradle.kts"));
            files.add(new File(root.getProjectDir(), "gradle.properties"));
            directories.add(new File(root.getProjectDir(), "gradle"));
            for (Project project : root.getAllprojects()) {
                files.add(project.getBuildFile());
                files.add(new File(project.getProjectDir(), "gradle.properties"));
                SourceSetContainer sets = project.getExtensions().findByType(SourceSetContainer.class);
                if (sets != null) for (SourceSet set : sets) {
                    for (File source : set.getAllSource().getSrcDirs()) {
                        if (!source.toPath().startsWith(project.getBuildDir().toPath())) sources.add(source);
                    }
                }
            }
            add(facts, "file.", files);
            add(facts, "directory.", directories);
            add(facts, "source.", sources);
            Path destination = output.getParent().resolve("gradle-configuration-inputs");
            Files.createDirectories(destination);
            String name = UUID.nameUUIDFromBytes(facts.getProperty("root").getBytes(StandardCharsets.UTF_8)).toString();
            try (Writer writer = Files.newBufferedWriter(destination.resolve(name + ".properties"), StandardCharsets.UTF_8)) {
                facts.store(writer, "Gradle evaluated build inputs");
            }
        } catch (IOException error) {
            throw new GradleException("Unable to record Gradle configuration inputs", error);
        }
    }

    static Map<String, Object> collect(Project primary, Path output, Set<File> scripts) throws IOException {
        Set<String> files = new LinkedHashSet<>();
        Set<String> directories = new LinkedHashSet<>();
        synchronized (scripts) {
            for (File script : scripts) files.add(script.getCanonicalPath());
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
        return result;
    }

    private static void add(Properties facts, String prefix, List<File> files) throws IOException {
        int index = 0;
        for (File file : files) facts.setProperty(prefix + index++, file.getCanonicalPath());
    }
}
