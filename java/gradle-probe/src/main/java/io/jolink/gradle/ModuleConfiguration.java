package io.jolink.gradle;

import java.io.File;
import java.lang.reflect.Method;
import java.net.URLClassLoader;
import java.nio.charset.Charset;
import java.util.*;
import org.gradle.api.Project;
import org.gradle.api.file.FileCollection;
import org.gradle.api.tasks.compile.JavaCompile;
import org.gradle.api.tasks.bundling.Jar;

/** Read the evaluated Gradle modularity policy, without running compilation. */
final class ModuleConfiguration {
    static Map<String, Object> compile(JavaCompile task) throws Exception {
        Map<String, Object> result = new LinkedHashMap<>();
        File source = descriptor(task);
        boolean module = task.getModularity().getInferModulePath().get() && source != null;
        // inferModulePath controls dependency placement, not whether javac
        // compiles an explicitly supplied module-info.java as a named module.
        result.put("moduleDescriptor", source == null ? null : source.getCanonicalPath());
        result.put("moduleName", source == null ? null : name(task.getProject(), source, task.getOptions().getEncoding()));
        Method access = JavaCompile.class.getDeclaredMethod("getJavaModuleDetector");
        access.setAccessible(true);
        Object detector = access.invoke(task);
        Object path = detector.getClass().getMethod("inferModulePath", boolean.class, FileCollection.class)
                .invoke(detector, module, task.getClasspath());
        Set<File> selected = new LinkedHashSet<>(((FileCollection) path).getFiles());
        if (module) for (Project producer : task.getProject().getRootProject().getAllprojects()) {
            Object compile = producer.getTasks().findByName("compileJava");
            if (!(compile instanceof JavaCompile) || descriptor((JavaCompile) compile) == null) continue;
            File output = ((JavaCompile) compile).getDestinationDirectory().get().getAsFile();
            if (task.getClasspath().getFiles().contains(output)) selected.add(output);
            Object jar = producer.getTasks().findByName("jar");
            if (jar instanceof Jar) {
                File archive = ((Jar) jar).getArchiveFile().get().getAsFile();
                if (task.getClasspath().getFiles().contains(archive)) selected.add(archive);
            }
        }
        List<String> modules = new ArrayList<>();
        for (File file : task.getClasspath()) if (selected.contains(file)) modules.add(file.getCanonicalPath());
        result.put("modulePath", modules);
        return result;
    }

    private static File descriptor(JavaCompile task) {
        for (File source : task.getSource()) if (source.getName().equals("module-info.java")) return source;
        return null;
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private static String name(Project project, File source, String encoding) throws Exception {
        File[] jars = new File(project.getGradle().getGradleHomeDir(), "lib").listFiles(
                file -> file.getName().startsWith("javaparser-core-") && file.getName().endsWith(".jar"));
        if (jars == null || jars.length == 0) throw new IllegalStateException("Gradle's Java source parser is unavailable");
        try (URLClassLoader loader = new URLClassLoader(new java.net.URL[]{jars[0].toURI().toURL()}, ModuleConfiguration.class.getClassLoader())) {
            Class<?> configuration = loader.loadClass("com.github.javaparser.ParserConfiguration");
            Class<?> level = loader.loadClass("com.github.javaparser.ParserConfiguration$LanguageLevel");
            Object config = configuration.getConstructor().newInstance();
            configuration.getMethod("setLanguageLevel", level).invoke(config, Enum.valueOf((Class)level, "JAVA_9"));
            configuration.getMethod("setCharacterEncoding", Charset.class).invoke(config,
                    encoding == null ? Charset.defaultCharset() : Charset.forName(encoding));
            Class<?> parserType = loader.loadClass("com.github.javaparser.JavaParser");
            Object parser = parserType.getConstructor(configuration).newInstance(config);
            Object parsed = parserType.getMethod("parse", File.class).invoke(parser, source);
            Object unit = ((Optional<?>) parsed.getClass().getMethod("getResult").invoke(parsed)).get();
            Object module = ((Optional<?>) unit.getClass().getMethod("getModule").invoke(unit)).get();
            return (String) module.getClass().getMethod("getNameAsString").invoke(module);
        }
    }
}
