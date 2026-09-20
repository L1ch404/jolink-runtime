package net.jolink.runtime.jdt;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Properties;

import org.eclipse.core.runtime.QualifiedName;
import org.eclipse.jdt.apt.core.internal.AnnotationProcessorFactoryLoader;
import org.eclipse.jdt.apt.core.internal.IServiceFactory;
import org.eclipse.jdt.apt.core.internal.util.FactoryPath;
import org.eclipse.jdt.apt.core.util.AptConfig;
import org.eclipse.jdt.core.IJavaProject;

/** Project-scoped APT options and explicit selection, using Eclipse-owned loaders. */
final class ProcessorConfiguration {
    private static final QualifiedName NAMES = new QualifiedName("net.jolink.runtime.jdt", "processorNames");
    private static final QualifiedName FLAGS = new QualifiedName("net.jolink.runtime.jdt", "processorFlags");

    static void configure(IJavaProject project, Path settings) throws Exception {
        Properties properties = new Properties();
        try (java.io.Reader reader = Files.newBufferedReader(settings, StandardCharsets.UTF_8)) {
            properties.load(reader);
        }
        Map<String, String> options = new LinkedHashMap<>();
        java.util.List<String> flags = new java.util.ArrayList<>();
        for (String key : properties.stringPropertyNames()) {
            if (key.startsWith("option.")) options.put(key.substring(7), properties.getProperty(key));
            if (key.startsWith("flag.")) {
                flags.add(key.substring(5));
                options.put(key.substring(5), "");
            }
        }
        AptConfig.setProcessorOptions(options, project);
        project.getProject().setPersistentProperty(NAMES, properties.getProperty("names", ""));
        project.getProject().setPersistentProperty(FLAGS, String.join(",", flags));
    }

    static void restoreFlags(IJavaProject project,
            org.eclipse.jdt.internal.compiler.apt.dispatch.BaseProcessingEnvImpl environment) {
        try {
            String flags = project.getProject().getPersistentProperty(FLAGS);
            if (flags == null || flags.isEmpty()) return;
            // JDT 3.25's option-placeholder replacement calls matcher(null)
            // for a valid -Aflag. Let Eclipse resolve string options normally,
            // then restore null flags in its environment cache before init().
            Map<String, String> options = new LinkedHashMap<>(environment.getOptions());
            for (String flag : flags.split(",")) options.put(flag, null);
            Field cache = org.eclipse.jdt.internal.compiler.apt.dispatch.BaseProcessingEnvImpl.class
                .getDeclaredField("_processorOptions");
            cache.setAccessible(true);
            cache.set(environment, java.util.Collections.unmodifiableMap(options));
        } catch (Exception error) {
            throw new IllegalArgumentException("Cannot configure annotation processor flags.", error);
        }
    }

    @SuppressWarnings("unchecked")
    static void select(IJavaProject project) {
        try {
            String names = project.getProject().getPersistentProperty(NAMES);
            if (names == null || names.isEmpty()) return;
            AnnotationProcessorFactoryLoader owner = AnnotationProcessorFactoryLoader.getLoader();
            // JDT 3.25 has no public named-selection API in IDE mode. Reuse its
            // loading path and loader lifetime, but install only the requested
            // factories, before service discovery/constructors/init can run.
            Map<IJavaProject, ClassLoader> loaders = (Map<IJavaProject, ClassLoader>) field("_iterativeLoaders").get(owner);
            ClassLoader loader = loaders.get(project);
            if (loader == null) {
                Method create = AnnotationProcessorFactoryLoader.class.getDeclaredMethod("_createIterativeClassLoader", Map.class);
                create.setAccessible(true);
                loader = (ClassLoader) create.invoke(null, ((FactoryPath) AptConfig.getFactoryPath(project)).getEnabledContainers());
                loaders.put(project, loader);
            }
            Map<IServiceFactory, FactoryPath.Attributes> factories = new LinkedHashMap<>();
            for (String name : names.split(",")) {
                Class<?> type = loader.loadClass(name);
                factories.put(() -> {
                    try { return type.getDeclaredConstructor().newInstance(); }
                    catch (ReflectiveOperationException error) { throw new IllegalArgumentException("Cannot create selected annotation processor: " + name, error); }
                }, new FactoryPath.Attributes(true, false));
            }
            Map<IJavaProject, Map<IServiceFactory, FactoryPath.Attributes>> cache =
                (Map<IJavaProject, Map<IServiceFactory, FactoryPath.Attributes>>) field("_project2Java6Factories").get(owner);
            cache.put(project, factories);
            // The Java-5 participant also consults this loader. Mark its
            // factories empty so it cannot rediscover the whole service path
            // and overwrite the explicit Java-6 selection during the build.
            ((Map<IJavaProject, Map<?, ?>>) field("_project2Java5Factories").get(owner))
                .put(project, java.util.Collections.emptyMap());
        } catch (Exception error) {
            throw new IllegalArgumentException("Cannot configure selected annotation processors.", error);
        }
    }

    private static Field field(String name) throws ReflectiveOperationException {
        Field field = AnnotationProcessorFactoryLoader.class.getDeclaredField(name);
        field.setAccessible(true);
        return field;
    }
}
