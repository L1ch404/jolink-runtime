package io.jolink.gradle;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.PosixFilePermission;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Collections;
import java.util.Comparator;
import java.util.EnumSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

import org.gradle.api.Action;
import org.gradle.api.Plugin;
import org.gradle.api.Project;
import org.gradle.api.Task;
import org.gradle.api.invocation.Gradle;
import org.gradle.api.tasks.SourceSet;
import org.gradle.api.tasks.SourceSetContainer;
import org.gradle.api.tasks.compile.JavaCompile;
import org.gradle.api.tasks.testing.Test;
import org.gradle.api.tasks.testing.junitplatform.JUnitPlatformOptions;
import org.gradle.api.tasks.testing.testng.TestNGOptions;
import org.gradle.jvm.toolchain.JavaCompiler;

/** Private init-plugin spike that exports facts from evaluated Gradle tasks. */
public final class JoLinkGradleInitPlugin implements Plugin<Gradle> {
    private static final String SCHEMA = "jolink.gradle-build-world-probe.v1";
    private static final String VERSION = "0.1.0-spike1";
    private final Map<String, Map<String, String>> baselineEnvironments =
            new LinkedHashMap<>();

    @Override
    public void apply(final Gradle gradle) {
        final String targetPath = System.getProperty(
                "jolink.gradle.targetProject", ":");
        gradle.beforeProject(new Action<Project>() {
            @Override
            public void execute(final Project project) {
                if (!targetPath.equals(project.getPath())) {
                    return;
                }
                project.getPluginManager().withPlugin(
                        "java",
                        ignored -> configureProject(project));
            }
        });
    }

    private void configureProject(final Project project) {
        project.getTasks().withType(Test.class).all(task ->
                baselineEnvironments.put(
                        task.getPath(), stringify(task.getEnvironment())));
        registerExportTask(project);
    }

    private void registerExportTask(final Project project) {
        if (project.getTasks().findByName("jolinkExportBuildWorld") != null) {
            return;
        }
        Task task = project.getTasks().create("jolinkExportBuildWorld");
        task.setGroup("joLink");
        task.setDescription("Export a private task-native Java Test Build World.");
        task.dependsOn("classes", "testClasses");
        task.doLast(ignored -> export(project));
    }

    private void export(Project project) {
        String rawOutput = System.getProperty("jolink.gradle.output");
        if (rawOutput == null || rawOutput.trim().isEmpty()) {
            throw new IllegalArgumentException("Missing jolink.gradle.output.");
        }
        Path output = new File(rawOutput).toPath().toAbsolutePath().normalize();
        Path started = output.resolveSibling(output.getFileName() + ".started");
        try {
            Files.createDirectories(output.getParent());
            Files.write(started, "started\n".getBytes(StandardCharsets.UTF_8));
            restrict(started);
            long slowMillis = Long.parseLong(System.getProperty(
                    "jolink.gradle.slowMillis", "0"));
            if (slowMillis > 0) {
                Thread.sleep(Math.min(slowMillis, 120_000L));
            }
            String json = render(project);
            Path temporary = output.resolveSibling(
                    "." + output.getFileName() + ".tmp");
            Files.write(temporary, json.getBytes(StandardCharsets.UTF_8));
            restrict(temporary);
            try {
                Files.move(
                        temporary,
                        output,
                        StandardCopyOption.ATOMIC_MOVE,
                        StandardCopyOption.REPLACE_EXISTING);
            } catch (AtomicMoveNotSupportedException ignored) {
                Files.move(
                        temporary,
                        output,
                        StandardCopyOption.REPLACE_EXISTING);
            }
            restrict(output);
        } catch (Exception error) {
            throw new RuntimeException("Unable to export joLink Build World.", error);
        }
    }

    private String render(Project project) throws Exception {
        SourceSetContainer sourceSets = project.getExtensions()
                .getByType(SourceSetContainer.class);
        SourceSet main = sourceSets.getByName(SourceSet.MAIN_SOURCE_SET_NAME);
        SourceSet test = sourceSets.getByName(SourceSet.TEST_SOURCE_SET_NAME);
        JavaCompile compileJava = (JavaCompile) project.getTasks()
                .getByName(main.getCompileJavaTaskName());
        JavaCompile compileTestJava = (JavaCompile) project.getTasks()
                .getByName(test.getCompileJavaTaskName());
        Test testTask = (Test) project.getTasks().getByName("test");

        Map<String, Object> root = new LinkedHashMap<>();
        root.put("schema", SCHEMA);
        root.put("probeVersion", VERSION);
        root.put("gradleVersion", project.getGradle().getGradleVersion());
        root.put("projectPath", project.getPath());
        root.put("projectName", project.getName());
        root.put("projectDirectory", canonical(project.getProjectDir()));
        root.put("rootDirectory", canonical(project.getRootDir()));
        root.put("gradleDaemonJavaHome", canonical(new File(
                System.getProperty("java.home"))));
        root.put("main", sourceSet(main));
        root.put("test", sourceSet(test));
        root.put("compileJava", compileTask(compileJava));
        root.put("compileTestJava", compileTask(compileTestJava));
        root.put("testRuntime", testTask(testTask));
        return json(root) + "\n";
    }

    private static Map<String, Object> sourceSet(SourceSet sourceSet)
            throws IOException {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("name", sourceSet.getName());
        result.put("javaSourceDirectories", files(
                sourceSet.getJava().getSrcDirs()));
        result.put("resourceDirectories", files(
                sourceSet.getResources().getSrcDirs()));
        result.put("classesDirectories", files(
                sourceSet.getOutput().getClassesDirs().getFiles()));
        result.put("resourcesDirectory", nullableFile(
                sourceSet.getOutput().getResourcesDir()));
        result.put("compileClasspath", files(
                sourceSet.getCompileClasspath().getFiles()));
        result.put("runtimeClasspath", files(
                sourceSet.getRuntimeClasspath().getFiles()));
        return result;
    }

    private static Map<String, Object> compileTask(JavaCompile task)
            throws Exception {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("taskPath", task.getPath());
        result.put("sourceCompatibility", task.getSourceCompatibility());
        result.put("targetCompatibility", task.getTargetCompatibility());
        result.put("sourceFiles", files(task.getSource().getFiles()));
        result.put("classpath", files(task.getClasspath().getFiles()));
        result.put("destinationDirectory", canonical(
                task.getDestinationDirectory().get().getAsFile()));
        result.put("encoding", task.getOptions().getEncoding());
        result.put("compilerArgsPrivate", new ArrayList<>(
                task.getOptions().getCompilerArgs()));
        result.put("compilerArgsIdentity", identity(
                task.getOptions().getCompilerArgs()));
        result.put("annotationProcessorPath", files(
                task.getOptions().getAnnotationProcessorPath() == null
                        ? Collections.<File>emptySet()
                        : task.getOptions().getAnnotationProcessorPath().getFiles()));
        result.put("generatedSourceOutputDirectory", providerFile(
                task.getOptions().getGeneratedSourceOutputDirectory()));
        result.put("release", task.getOptions().getRelease().getOrNull());
        JavaCompiler compiler = task.getJavaCompiler().getOrNull();
        result.put("compilerJavaHome", compiler == null ? null : canonical(
                compiler.getMetadata().getInstallationPath().getAsFile()));
        result.put("compilerJavaVersion", compiler == null ? null
                : compiler.getMetadata().getLanguageVersion().asInt());
        return result;
    }

    private Map<String, Object> testTask(Test task) throws Exception {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("taskPath", task.getPath());
        result.put("framework", framework(task));
        result.put("testClassesDirectories", files(
                task.getTestClassesDirs().getFiles()));
        result.put("classpath", files(task.getClasspath().getFiles()));
        result.put("workingDirectory", canonical(task.getWorkingDir()));
        result.put("enableAssertions", task.getEnableAssertions());
        List<String> jvmArgs = new ArrayList<>(task.getJvmArgs());
        result.put("jvmArgsPrivate", jvmArgs);
        result.put("jvmArgsIdentity", identity(jvmArgs));

        Map<String, String> properties = stringify(task.getSystemProperties());
        result.put("systemPropertiesPrivate", properties);
        result.put("systemPropertyNames", new ArrayList<>(properties.keySet()));
        result.put("systemPropertiesIdentity", identity(properties));

        Map<String, String> environment = stringify(task.getEnvironment());
        Map<String, String> baseline = baselineEnvironments.get(task.getPath());
        if (baseline == null) {
            throw new IllegalStateException(
                    "Test task environment baseline was not captured.");
        }
        Map<String, String> overrides = environmentDifference(
                environment, baseline);
        result.put("environmentOverridesPrivate", overrides);
        result.put("environmentOverrideNames", new ArrayList<>(overrides.keySet()));
        result.put("environmentOverridesIdentity", identity(overrides));

        result.put("includePatterns", sorted(
                task.getFilter().getIncludePatterns()));
        result.put("excludePatterns", sorted(
                task.getFilter().getExcludePatterns()));
        return result;
    }

    private static String framework(Test task) {
        if (task.getOptions() instanceof JUnitPlatformOptions) {
            return "junit_platform";
        }
        if (task.getOptions() instanceof TestNGOptions) {
            return "testng";
        }
        return "junit4";
    }

    private static Map<String, String> environmentDifference(
            Map<String, String> effective,
            Map<String, String> baseline) {
        Set<String> names = new LinkedHashSet<>();
        names.addAll(effective.keySet());
        names.addAll(baseline.keySet());
        List<String> ordered = new ArrayList<>(names);
        Collections.sort(ordered);
        Map<String, String> result = new LinkedHashMap<>();
        for (String name : ordered) {
            if (!Objects.equals(effective.get(name), baseline.get(name))) {
                result.put(name, effective.get(name));
            }
        }
        return result;
    }

    private static Map<String, String> stringify(Map<?, ?> values) {
        List<String> names = new ArrayList<>();
        for (Object name : values.keySet()) {
            names.add(String.valueOf(name));
        }
        Collections.sort(names);
        Map<String, String> result = new LinkedHashMap<>();
        for (String name : names) {
            Object value = values.get(name);
            result.put(name, value == null ? null : String.valueOf(value));
        }
        return result;
    }

    private static List<String> files(Collection<File> values)
            throws IOException {
        List<String> result = new ArrayList<>();
        for (File value : values) {
            result.add(canonical(value));
        }
        Collections.sort(result);
        return result;
    }

    private static List<String> sorted(Collection<String> values) {
        List<String> result = new ArrayList<>(values);
        Collections.sort(result);
        return result;
    }

    private static String nullableFile(File value) throws IOException {
        return value == null ? null : canonical(value);
    }

    private static String providerFile(
            org.gradle.api.file.DirectoryProperty value) throws IOException {
        return value.isPresent() ? canonical(value.get().getAsFile()) : null;
    }

    private static String canonical(File value) throws IOException {
        return value.getCanonicalFile().toPath().toString();
    }

    private static String identity(Object value) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        digest.update(json(value).getBytes(StandardCharsets.UTF_8));
        return "sha256:" + hex(digest.digest());
    }

    private static String hex(byte[] bytes) {
        char[] alphabet = "0123456789abcdef".toCharArray();
        char[] result = new char[bytes.length * 2];
        for (int index = 0; index < bytes.length; index++) {
            int value = bytes[index] & 0xff;
            result[index * 2] = alphabet[value >>> 4];
            result[index * 2 + 1] = alphabet[value & 0x0f];
        }
        return new String(result);
    }

    private static String json(Object value) {
        if (value == null) {
            return "null";
        }
        if (value instanceof Boolean || value instanceof Number) {
            return String.valueOf(value);
        }
        if (value instanceof Map) {
            StringBuilder out = new StringBuilder("{");
            boolean first = true;
            for (Object raw : ((Map<?, ?>) value).entrySet()) {
                Map.Entry<?, ?> entry = (Map.Entry<?, ?>) raw;
                if (!first) {
                    out.append(',');
                }
                first = false;
                out.append(json(String.valueOf(entry.getKey())))
                        .append(':').append(json(entry.getValue()));
            }
            return out.append('}').toString();
        }
        if (value instanceof Iterable) {
            StringBuilder out = new StringBuilder("[");
            boolean first = true;
            for (Object item : (Iterable<?>) value) {
                if (!first) {
                    out.append(',');
                }
                first = false;
                out.append(json(item));
            }
            return out.append(']').toString();
        }
        String text = String.valueOf(value);
        StringBuilder out = new StringBuilder(text.length() + 2).append('"');
        for (int index = 0; index < text.length(); index++) {
            char current = text.charAt(index);
            switch (current) {
                case '"': out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\b': out.append("\\b"); break;
                case '\f': out.append("\\f"); break;
                case '\n': out.append("\\n"); break;
                case '\r': out.append("\\r"); break;
                case '\t': out.append("\\t"); break;
                default:
                    if (current < 0x20) {
                        out.append(String.format("\\u%04x", (int) current));
                    } else {
                        out.append(current);
                    }
            }
        }
        return out.append('"').toString();
    }

    private static void restrict(Path path) {
        try {
            Files.setPosixFilePermissions(
                    path,
                    EnumSet.of(
                            PosixFilePermission.OWNER_READ,
                            PosixFilePermission.OWNER_WRITE));
        } catch (Exception ignored) {
            // Windows ACL inheritance is verified by the product integration.
        }
    }
}
