package net.jolink.runtime.jdt;

import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;

/** Keep Lombok's own configuration lookup attached to original source locations. */
final class LombokConfigurationBridge {
    private static LombokConfigurationBridge active;
    private static String inactiveState = response(java.util.Collections.emptyList());
    private final ClassLoader loader;
    private final Class<?> configuration;
    private final Class<?> factoryType;
    private final Class<?> fileType;
    private final Class<?> mapperType;
    private final Class<?> resolverType;
    private final Class<?> keyType;
    private final Method parsed;
    private final Method description;
    private final Method resolveFile;
    private final Method forUri;
    private final Method forDirectory;
    private final Method resolveValue;
    private final Constructor<?> resolverConstructor;
    private final Object cache;
    private final Object nativeMapper;
    private final Map<String, Object> files = new LinkedHashMap<>();
    private final Map<String, Set<String>> missingImports = new LinkedHashMap<>();
    private final Map<Path, Path> originals;
    private final Map<Path, Path> generatedRoots;
    private final Set<Path> sourceDirectories;

    private LombokConfigurationBridge(Map<Path, Path> originals, Map<Path, Path> generatedRoots,
            Set<Path> sourceDirectories) throws Exception {
        this.originals = originals;
        this.generatedRoots = generatedRoots;
        this.sourceDirectories = sourceDirectories;
        // Use the patched compiler bundle, not JDT Core or the system agent:
        // Equinox gives those loaders separate copies of Lombok's statics.
        configuration = Class.forName("lombok.core.LombokConfiguration", true,
                org.eclipse.jdt.internal.compiler.parser.Parser.class.getClassLoader());
        loader = configuration.getClassLoader();
        factoryType = loader.loadClass("lombok.core.configuration.ConfigurationResolverFactory");
        fileType = loader.loadClass("lombok.core.configuration.ConfigurationFile");
        mapperType = loader.loadClass("lombok.core.configuration.ConfigurationFileToSource");
        resolverType = loader.loadClass("lombok.core.configuration.ConfigurationResolver");
        keyType = loader.loadClass("lombok.core.configuration.ConfigurationKey");
        loader.loadClass("lombok.core.configuration.ConfigurationKeysLoader$LoaderLoader")
                .getMethod("loadAllConfigurationKeys").invoke(null);
        Class<?> cacheType = loader.loadClass("lombok.core.configuration.FileSystemSourceCache");
        Class<?> parserType = loader.loadClass("lombok.core.configuration.ConfigurationParser");
        Class<?> reporterType = loader.loadClass("lombok.core.configuration.ConfigurationProblemReporter");
        cache = cacheType.getConstructor().newInstance();
        forUri = cacheType.getMethod("forUri", URI.class);
        forDirectory = fileType.getMethod("forDirectory", java.io.File.class);
        parsed = mapperType.getMethod("parsed", fileType);
        description = fileType.getDeclaredMethod("description");
        description.setAccessible(true);
        resolveFile = fileType.getMethod("resolve", String.class);
        resolveValue = resolverType.getMethod("resolve", keyType);
        resolverConstructor = loader.loadClass("lombok.core.configuration.BubblingConfigurationResolver")
                .getConstructor(fileType, mapperType);
        Object console = reporterType.getField("CONSOLE").get(null);
        Method report = reporterType.getMethod("report", String.class, String.class, int.class, CharSequence.class);
        Object reporter = Proxy.newProxyInstance(loader, new Class<?>[]{reporterType}, (proxy, method, arguments) -> {
            // Native parsing has already recognized this import, but excludes a
            // missing file from parsed-source imports. Track its future creation.
            if ("Imported file does not exist".equals(arguments[1]) && arguments[3] != null) {
                Object context = files.get(arguments[0]);
                if (context != null) {
                    String line = arguments[3].toString().trim();
                    Object imported = invoke(resolveFile, context, line.substring("import".length()).trim());
                    if (imported != null) missingImports.computeIfAbsent((String) arguments[0], key -> new TreeSet<>())
                            .add(physicalPath(imported));
                }
            }
            return invoke(report, console, arguments);
        });
        Object parser = parserType.getConstructor(reporterType).newInstance(reporter);
        nativeMapper = cacheType.getMethod("fileToSource", parserType).invoke(cache, parser);
    }

    static String configure(Path mapping) throws Exception {
        Map<Path, Path> originals = new LinkedHashMap<>();
        Map<Path, Path> generatedRoots = new LinkedHashMap<>();
        Set<Path> rememberedDirectories = new TreeSet<>();
        for (String line : Files.readAllLines(mapping, StandardCharsets.UTF_8)) {
            if (line.isEmpty()) continue;
            String[] parts = line.split("\t", -1);
            if ("root".equals(parts[0])) {
                generatedRoots.put(decodePath(parts[1]), decodePath(parts[2]));
            } else if ("directory".equals(parts[0])) {
                rememberedDirectories.add(decodePath(parts[1]));
            } else {
                originals.put(decodePath(parts[0]), decodePath(parts[1]));
            }
        }
        Set<Path> sourceDirectories = directories(originals);
        rememberedDirectories.addAll(generatedRoots.values());
        sourceDirectories.addAll(rememberedDirectories);
        if (System.getProperty("lombok.disableConfig") != null) {
            List<String> groups = new ArrayList<>();
            for (Path directory : sourceDirectories) {
                groups.add(groupJson(directory, "disabled", new TreeSet<>()));
            }
            active = null;
            inactiveState = response(groups);
            return inactiveState;
        }
        LombokConfigurationBridge bridge = new LombokConfigurationBridge(originals, generatedRoots, sourceDirectories);
        List<String> groups = bridge.groups();
        Object factory = Proxy.newProxyInstance(bridge.loader, new Class<?>[]{bridge.factoryType},
                (proxy, method, arguments) -> {
                    URI source = (URI) arguments[0];
                    Path original = null;
                    if (source != null && "file".equalsIgnoreCase(source.getScheme())) {
                        original = bridge.originalSource(Paths.get(source).toAbsolutePath().normalize());
                    }
                    Object configurationFile = original == null
                            ? invoke(bridge.forUri, bridge.cache, source)
                            : invoke(bridge.forDirectory, null, original.getParent().toFile());
                    return bridge.resolver(configurationFile, null);
                });
        bridge.configuration.getMethod("overrideConfigurationResolverFactory", bridge.factoryType).invoke(null, factory);
        active = bridge;
        return response(groups);
    }

    static String state() throws Exception {
        return active == null ? inactiveState : response(active.groups());
    }

    private Path originalSource(Path privateSource) {
        Path original = originals.get(privateSource);
        if (original != null) return original;
        Path selected = null;
        for (Path root : generatedRoots.keySet()) {
            if (privateSource.startsWith(root) && (selected == null || root.getNameCount() > selected.getNameCount())) {
                selected = root;
            }
        }
        if (selected == null) return null;
        original = generatedRoots.get(selected).resolve(selected.relativize(privateSource));
        // APT files appear during this build, after the original-source snapshot.
        // Remember their actual logical directories for configuration invalidation.
        sourceDirectories.add(original.getParent());
        return original;
    }

    private List<String> groups() throws Exception {
        @SuppressWarnings("unchecked")
        Map<String, Object> keys = (Map<String, Object>) keyType.getMethod("registeredKeys").invoke(null);
        List<String> groups = new ArrayList<>();
        for (Path directory : sourceDirectories) {
            Set<String> inputs = new TreeSet<>();
            // forUri requires an existing source file; logical APT output has
            // not been written to the native build directory. Start the same
            // native resolver at its directory without creating business files.
            Object resolver = resolver(invoke(forDirectory, null, directory.toFile()), inputs);
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            for (Map.Entry<String, Object> key : new TreeMap<>(keys).entrySet()) {
                addDigest(digest, key.getKey());
                addDigest(digest, invoke(resolveValue, resolver, key.getValue()));
            }
            StringBuilder hash = new StringBuilder();
            for (byte value : digest.digest()) hash.append(String.format("%02x", value & 0xff));
            groups.add(groupJson(directory, hash.toString(), inputs));
        }
        return groups;
    }

    private Object resolver(Object configurationFile, Set<String> inputs) throws Exception {
        Object mapper = nativeMapper;
        if (inputs != null) mapper = Proxy.newProxyInstance(loader, new Class<?>[]{mapperType}, (proxy, method, arguments) -> {
            Object file = arguments[0];
            String name = (String) invoke(description, file);
            files.put(name, file);
            inputs.add(physicalPath(file));
            Object result = invoke(parsed, nativeMapper, file);
            inputs.addAll(missingImports.getOrDefault(name, java.util.Collections.emptySet()));
            return result;
        });
        return resolverConstructor.newInstance(configurationFile, mapper);
    }

    private String physicalPath(Object file) throws Exception {
        // Archive descriptors include !entry; inspect the backing file instead
        // of splitting arbitrary user paths which can themselves contain '!'.
        for (String fieldName : new String[]{"archive", "file"}) {
            try {
                Field field = file.getClass().getDeclaredField(fieldName);
                if (field.getType() != java.io.File.class) continue;
                field.setAccessible(true);
                return ((java.io.File) field.get(file)).toPath().toAbsolutePath().normalize().toString();
            } catch (NoSuchFieldException ignored) {
                // Older Lombok versions can still expose a regular identifier.
            }
        }
        return Paths.get((String) invoke(description, file)).toAbsolutePath().normalize().toString();
    }

    private static Set<Path> directories(Map<Path, Path> originals) {
        Set<Path> result = new TreeSet<>();
        for (Path source : originals.values()) result.add(source.getParent());
        return result;
    }

    private static Path decodePath(String value) {
        return Paths.get(new String(Base64.getDecoder().decode(value), StandardCharsets.UTF_8)).toAbsolutePath().normalize();
    }

    private static void addDigest(MessageDigest digest, Object value) {
        if (value == null) { digest.update((byte) 0); return; }
        if (value instanceof Collection<?>) {
            digest.update((byte) 1);
            addDigest(digest, ((Collection<?>) value).size());
            for (Object item : (Collection<?>) value) addDigest(digest, item);
            return;
        }
        digest.update((byte) 2);
        byte[] bytes = value.toString().getBytes(StandardCharsets.UTF_8);
        digest.update(Integer.toString(bytes.length).getBytes(StandardCharsets.US_ASCII));
        digest.update((byte) ':');
        digest.update(bytes);
    }

    private static Object invoke(Method method, Object target, Object... arguments) throws Exception {
        try { return method.invoke(target, arguments); }
        catch (InvocationTargetException error) {
            Throwable cause = error.getCause();
            if (cause instanceof Exception) throw (Exception) cause;
            throw error;
        }
    }

    private static String groupJson(Path directory, String fingerprint, Set<String> inputs) {
        return "{\"directory\":" + WorkerApplication.json(directory.toString())
                + ",\"fingerprint\":" + WorkerApplication.json(fingerprint)
                + ",\"inputs\":" + WorkerApplication.jsonArray(new ArrayList<>(inputs)) + "}";
    }

    private static String response(List<String> groups) {
        return "{\"ok\":true,\"status\":\"lombok_configured\",\"groups\":"
                + WorkerApplication.jsonObjectsArray(groups) + "}";
    }
}
