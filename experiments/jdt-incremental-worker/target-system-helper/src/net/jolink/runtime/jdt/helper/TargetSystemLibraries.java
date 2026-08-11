package net.jolink.runtime.jdt.helper;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.util.Base64;

/**
 * Runs under the exact target JDK 8 and captures its active boot class path in
 * JVM order. It deliberately does not scan the JRE directory.
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
        String bootClassPath = System.getProperty("sun.boot.class.path");
        if (bootClassPath == null || bootClassPath.trim().isEmpty()) {
            throw new IllegalStateException("sun.boot.class.path is unavailable.");
        }
        String[] rawEntries = bootClassPath.split(
                java.util.regex.Pattern.quote(File.pathSeparator), -1);
        try (BufferedWriter writer = new BufferedWriter(new OutputStreamWriter(
                new FileOutputStream(args[0]), StandardCharsets.UTF_8))) {
            writer.write("format=jolink-target-system-libraries-v1");
            writer.newLine();
            writeEncoded(writer, "java.vendor", System.getProperty("java.vendor", ""));
            writeEncoded(writer, "java.version", version);
            writeEncoded(writer, "java.home", new File(
                    System.getProperty("java.home", "")).getCanonicalPath());
            writer.write("discovery.method=sun.boot.class.path-from-target-jdk8");
            writer.newLine();
            writer.write("entry.count=" + rawEntries.length);
            writer.newLine();
            for (int index = 0; index < rawEntries.length; index++) {
                File entry = new File(rawEntries[index]).getCanonicalFile();
                writeEncoded(writer, "entry." + index, entry.getPath());
                writer.write("entry." + index + ".present=" + entry.exists());
                writer.newLine();
            }
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
