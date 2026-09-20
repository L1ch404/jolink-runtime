package io.jolink.gradle;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import org.gradle.api.GradleException;
import org.gradle.api.Project;
import org.gradle.api.invocation.Gradle;
import org.gradle.api.tasks.SourceSet;
import org.gradle.api.tasks.SourceSetContainer;

/** Capture evaluated builds, including buildSrc and arbitrarily named included builds. */
final class GradleConfigurationInputs {
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

    static Map<String, Object> collect(Project primary, Path output) throws IOException {
        Set<String> files = new LinkedHashSet<>();
        Set<String> directories = new LinkedHashSet<>();
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
