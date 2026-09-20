package net.jolink.runtime.jdt;

import java.io.PrintStream;
import java.io.OutputStream;
import java.io.UnsupportedEncodingException;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Locale;
import org.eclipse.jdt.core.JavaCore;
import org.eclipse.jdt.internal.core.builder.IncrementalImageBuilder;
import org.eclipse.jdt.internal.core.builder.JavaBuilder;
import org.eclipse.osgi.service.debug.DebugOptions;
import org.osgi.framework.BundleContext;
import org.osgi.framework.FrameworkUtil;
import org.osgi.framework.ServiceReference;
import static net.jolink.runtime.jdt.WorkerApplication.json;

/** Capture the pinned JavaBuilder's actual decisions, not inferred fallbacks. */
final class BuildDecisionTrace {
    private static final int MAX_DECISIONS = 32;
    private static final Map<String, String[]> decisions = new LinkedHashMap<>();
    private static boolean active;
    private static boolean previousDebug;
    private static boolean truncated;
    private static String project = "";
    private static boolean traceEnabled;
    private static boolean verbose;

    private BuildDecisionTrace() { }

    static void install() throws UnsupportedEncodingException {
        String level = System.getenv("JOLINK_LOG_LEVEL");
        level = level == null ? "WARNING" : level.trim().toUpperCase(Locale.ROOT);
        traceEnabled = "INFO".equals(level) || "DEBUG".equals(level);
        verbose = "DEBUG".equals(level);
        // WorkerApplication has already captured the original stdout for JSON.
        // JavaBuilder (and processors) must never write text to that protocol.
        OutputStream sink = verbose ? System.err : new OutputStream() {
            @Override public void write(int value) { }
            @Override public void write(byte[] value, int offset, int length) { }
        };
        System.setOut(new PrintStream(sink, true, "UTF-8") {
            @Override public void println(String line) {
                record(line);
                if (verbose) super.println(line);
            }
        });
        if (traceEnabled) {
            // Modern JDT routes JavaBuilder messages through DebugTrace by
            // default. Use Eclipse's supported stdout option so the existing
            // capture receives both project names and build decisions.
            BundleContext context = FrameworkUtil.getBundle(BuildDecisionTrace.class).getBundleContext();
            ServiceReference<DebugOptions> reference = context.getServiceReference(DebugOptions.class);
            DebugOptions options = context.getService(reference);
            options.setDebugEnabled(true);
            options.setOption(JavaCore.PLUGIN_ID + "/debug", "true");
            options.setOption(JavaCore.PLUGIN_ID + "/debug/traceToStdOut", "true");
            context.ungetService(reference);
        }
    }

    static synchronized void begin() {
        decisions.clear();
        project = "";
        truncated = false;
        active = traceEnabled;
        previousDebug = JavaBuilder.DEBUG;
        JavaBuilder.DEBUG = traceEnabled;
    }

    static synchronized void end() {
        JavaBuilder.DEBUG = previousDebug;
        active = false;
    }

    private static synchronized void record(String line) {
        if (!active || line == null) return;
        line = line.trim();
        String start = "JavaBuilder: Starting build of";
        if (line.startsWith(start)) {
            String value = line.substring(start.length()).trim();
            int time = value.indexOf(" @");
            project = time < 0 ? value : value.substring(0, time);
            return;
        }
        if (!line.startsWith("JavaBuilder:")
                && !line.startsWith("ABORTING incremental build")
                && !line.startsWith("MUST DO FULL BUILD.")) return;
        String reason = null;
        if (line.contains("exceeded loop count")) reason = "INCREMENTAL_LOOP_LIMIT_EXCEEDED";
        else if (line.contains("last saved state was not found")) reason = "SAVED_STATE_NOT_FOUND";
        else if (line.contains("full build since classpath has changed")) reason = "CLASSPATH_CHANGED";
        else if (line.contains("deltas are missing after incremental request")) reason = "RESOURCE_DELTAS_MISSING";
        else if (line.contains("full build since project settings have changed")) reason = "PROJECT_SETTINGS_CHANGED";
        else if (line.contains("full build since there are structural deltas")) reason = "STRUCTURAL_DELTAS";
        else if (line.contains("full build since incremental build failed")) reason = "INCREMENTAL_BUILD_ABORTED";
        else if (line.contains("ABORTING incremental build... problem with")) reason = "TYPE_RENAME_ABORT";
        else if (line.contains("MUST DO FULL BUILD. Found change to class file")) reason = "OUTPUT_CLASS_CHANGED";
        else if (line.contains("Performing full build as requested")) reason = "BUILDER_RECEIVED_FULL";
        if (reason == null) return;
        String key = project + ":" + reason;
        if (decisions.containsKey(key)) return;
        if (decisions.size() >= MAX_DECISIONS) {
            truncated = true;
            return;
        }
        decisions.put(key, new String[] { project, reason });
    }

    static synchronized String snapshotJson() {
        StringBuilder result = new StringBuilder("{\"source\":")
                .append(json(traceEnabled ? "jdt_builder_trace" : "disabled"))
                .append(",\"decisions\":[");
        boolean first = true;
        for (String[] decision : decisions.values()) {
            if (!first) result.append(',');
            first = false;
            result.append("{\"project\":").append(json(decision[0]))
                    .append(",\"reason\":").append(json(decision[1])).append('}');
        }
        return result.append("],\"truncated\":").append(truncated)
                .append(",\"incremental_loop_limit\":")
                .append(IncrementalImageBuilder.MaxCompileLoop).append('}').toString();
    }
}
