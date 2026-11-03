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
    private final Method resolveValue;
    private final Constructor<?> resolverConstructor;
    private final Object cache;
    private final Object nativeMapper;
    private final Map<String, Object> files = new LinkedHashMap<>();
    private final Map<String, Set<String>> missingImports = new LinkedHashMap<>();
    private final Map<Path, Path> originals;

    private LombokConfigurationBridge(Map<Path, Path> originals) throws Exception {
        this.originals = originals;
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
        for (String line : Files.readAllLines(mapping, StandardCharsets.UTF_8)) {
            if (line.isEmpty()) continue;
            String[] parts = line.split("\t", -1);
            originals.put(decodePath(parts[0]), decodePath(parts[1]));
        }
        if (System.getProperty("lombok.disableConfig") != null) {
            List<String> groups = new ArrayList<>();
            for (Path directory : directories(originals).keySet()) {
                groups.add(groupJson(directory, "disabled", new TreeSet<>()));
            }
            return response(groups);
        }
        LombokConfigurationBridge bridge = new LombokConfigurationBridge(originals);
        List<String> groups = bridge.groups();
        Object factory = Proxy.newProxyInstance(bridge.loader, new Class<?>[]{bridge.factoryType},
                (proxy, method, arguments) -> {
                    URI source = (URI) arguments[0];
                    Path original = null;
                    if (source != null && "file".equalsIgnoreCase(source.getScheme())) {
                        original = bridge.originals.get(Paths.get(source).toAbsolutePath().normalize());
                    }
                    return bridge.resolver(original == null ? source : original.toUri(), null);
                });
        bridge.configuration.getMethod("overrideConfigurationResolverFactory", bridge.factoryType).invoke(null, factory);
        return response(groups);
    }

    private List<String> groups() throws Exception {
        @SuppressWarnings("unchecked")
        Map<String, Object> keys = (Map<String, Object>) keyType.getMethod("registeredKeys").invoke(null);
        List<String> groups = new ArrayList<>();
        for (Map.Entry<Path, Path> entry : directories(originals).entrySet()) {
            Set<String> inputs = new TreeSet<>();
            Object resolver = resolver(entry.getValue().toUri(), inputs);
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            for (Map.Entry<String, Object> key : new TreeMap<>(keys).entrySet()) {
                addDigest(digest, key.getKey());
                addDigest(digest, invoke(resolveValue, resolver, key.getValue()));
            }
            StringBuilder hash = new StringBuilder();
            for (byte value : digest.digest()) hash.append(String.format("%02x", value & 0xff));
            groups.add(groupJson(entry.getKey(), hash.toString(), inputs));
        }
        return groups;
    }

    private Object resolver(URI source, Set<String> inputs) throws Exception {
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
        return resolverConstructor.newInstance(invoke(forUri, cache, source), mapper);
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

    private static Map<Path, Path> directories(Map<Path, Path> originals) {
        Map<Path, Path> result = new TreeMap<>();
        for (Path source : originals.values()) result.putIfAbsent(source.getParent(), source);
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
