package net.jolink.runtime.jdt.helper;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import javax.tools.JavaCompiler;
import javax.tools.StandardJavaFileManager;
import javax.tools.StandardLocation;
import javax.tools.ToolProvider;

/**
 * Runs under the exact target JDK 8 and captures both the advertised platform
 * configuration and javac's effective platform class path. It deliberately
 * does not infer a JRE layout or scan a hard-coded lib directory.
 */
public final class TargetSystemLibraries {
    private TargetSystemLibraries() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            throw new IllegalArgumentException("Expected one output path.");
        }
        String version = System.getProperty("java.version", "");
        if (!version.startsWith("1.8.")) {
            throw new IllegalStateException("Target system helper requires JDK 8.");
        }

        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            throw new IllegalStateException("Target JDK does not expose javac.");
        }

        List<File> compilerPlatform = new ArrayList<>();
        try (StandardJavaFileManager fileManager = compiler.getStandardFileManager(
                null, null, StandardCharsets.UTF_8)) {
            Iterable<? extends File> platform = fileManager.getLocation(
                    StandardLocation.PLATFORM_CLASS_PATH);
            if (platform == null) {
                throw new IllegalStateException(
                        "javac platform class path is unavailable.");
            }
            for (File entry : platform) {
                compilerPlatform.add(entry.getCanonicalFile());
            }
        }
        if (compilerPlatform.isEmpty()) {
            throw new IllegalStateException("javac platform class path is empty.");
        }

        List<File> runtimeExtensionUrls = new ArrayList<>();
        ClassLoader extensionLoader = ClassLoader.getSystemClassLoader().getParent();
        if (extensionLoader instanceof URLClassLoader) {
            for (URL url : ((URLClassLoader) extensionLoader).getURLs()) {
                if ("file".equalsIgnoreCase(url.getProtocol())) {
                    runtimeExtensionUrls.add(
                            new File(url.toURI()).getCanonicalFile());
                }
            }
        }

        try (BufferedWriter writer = new BufferedWriter(new OutputStreamWriter(
                new FileOutputStream(args[0]), StandardCharsets.UTF_8))) {
            writer.write("format=jolink-target-system-libraries-v2");
            writer.newLine();
            writeEncoded(writer, "java.vendor", System.getProperty("java.vendor", ""));
            writeEncoded(writer, "java.version", version);
            writeEncoded(writer, "java.home", new File(
                    System.getProperty("java.home", "")).getCanonicalPath());
            writer.write(
                    "discovery.method=target-jdk8-javac-platform-class-path-and-properties");
            writer.newLine();

            writePropertyPaths(
                    writer,
                    "bootstrap.advertised",
                    System.getProperty("sun.boot.class.path", ""));
            writePropertyPaths(
                    writer,
                    "extension.directory",
                    System.getProperty("java.ext.dirs", ""));
            writePropertyPaths(
                    writer,
                    "endorsed.directory",
                    System.getProperty("java.endorsed.dirs", ""));
            writeFiles(writer, "compiler.platform", compilerPlatform);
            writeFiles(writer, "runtime.extension.url", runtimeExtensionUrls);
        }
    }

    private static void writePropertyPaths(
            BufferedWriter writer, String prefix, String property) throws Exception {
        List<File> paths = new ArrayList<>();
        if (property != null && !property.trim().isEmpty()) {
            String[] values = property.split(
                    java.util.regex.Pattern.quote(File.pathSeparator), -1);
            for (String value : values) {
                if (!value.trim().isEmpty()) {
                    paths.add(new File(value).getCanonicalFile());
                }
            }
        }
        writeFiles(writer, prefix, paths);
    }

    private static void writeFiles(
            BufferedWriter writer, String prefix, List<File> files) throws Exception {
        writer.write(prefix + ".count=" + files.size());
        writer.newLine();
        for (int index = 0; index < files.size(); index++) {
            File entry = files.get(index).getCanonicalFile();
            writeEncoded(writer, prefix + "." + index, entry.getPath());
            writer.write(prefix + "." + index + ".present=" + entry.exists());
            writer.newLine();
        }
    }

    private static void writeEncoded(
            BufferedWriter writer, String key, String value) throws Exception {
        writer.write(key);
        writer.write(".base64=");
        writer.write(Base64.getEncoder().encodeToString(
                value.getBytes(StandardCharsets.UTF_8)));
        writer.newLine();
    }
}
