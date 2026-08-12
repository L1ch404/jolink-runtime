package net.jolink.runtime.jdt;

import java.lang.management.ClassLoadingMXBean;
import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryPoolMXBean;
import java.lang.management.MemoryUsage;
import java.lang.management.RuntimeMXBean;
import java.lang.management.ThreadMXBean;
import java.util.ArrayList;
import java.util.List;

/** Bounded, read-only JVM metrics used by the isolated A9 experiment. */
final class WorkerMetrics {
    private WorkerMetrics() {
    }

    static void resetPeaks() {
        for (MemoryPoolMXBean pool : ManagementFactory.getMemoryPoolMXBeans()) {
            try {
                pool.resetPeakUsage();
            } catch (RuntimeException ignored) {
                // An unsupported pool is reported by the following snapshot.
            }
        }
    }

    static String snapshotJson(boolean gcRequestSent) {
        MemoryMXBean memory = ManagementFactory.getMemoryMXBean();
        MemoryUsage heap = memory.getHeapMemoryUsage();
        RuntimeMXBean runtime = ManagementFactory.getRuntimeMXBean();
        ThreadMXBean threads = ManagementFactory.getThreadMXBean();
        ClassLoadingMXBean classes = ManagementFactory.getClassLoadingMXBean();

        List<String> pools = new ArrayList<>();
        for (MemoryPoolMXBean pool : ManagementFactory.getMemoryPoolMXBeans()) {
            MemoryUsage usage = pool.getUsage();
            MemoryUsage peak = pool.getPeakUsage();
            pools.add("{\"name\":" + json(pool.getName())
                    + ",\"used_bytes\":" + value(usage, UsageValue.USED)
                    + ",\"committed_bytes\":" + value(usage, UsageValue.COMMITTED)
                    + ",\"max_bytes\":" + value(usage, UsageValue.MAX)
                    + ",\"peak_used_bytes\":" + value(peak, UsageValue.USED)
                    + "}");
        }

        List<String> collectors = new ArrayList<>();
        for (GarbageCollectorMXBean collector
                : ManagementFactory.getGarbageCollectorMXBeans()) {
            collectors.add("{\"name\":" + json(collector.getName())
                    + ",\"collection_count\":" + collector.getCollectionCount()
                    + ",\"collection_time_ms\":" + collector.getCollectionTime()
                    + "}");
        }

        return "{\"heap_used_bytes\":" + heap.getUsed()
                + ",\"heap_committed_bytes\":" + heap.getCommitted()
                + ",\"heap_max_bytes\":" + heap.getMax()
                + ",\"thread_count\":" + threads.getThreadCount()
                + ",\"peak_thread_count\":" + threads.getPeakThreadCount()
                + ",\"loaded_class_count\":" + classes.getLoadedClassCount()
                + ",\"total_loaded_class_count\":" + classes.getTotalLoadedClassCount()
                + ",\"unloaded_class_count\":" + classes.getUnloadedClassCount()
                + ",\"uptime_ms\":" + runtime.getUptime()
                + ",\"gc_request_sent\":" + gcRequestSent
                + ",\"memory_pools\":[" + String.join(",", pools) + "]"
                + ",\"garbage_collectors\":[" + String.join(",", collectors) + "]}"
                ;
    }

    private enum UsageValue {
        USED,
        COMMITTED,
        MAX
    }

    private static long value(MemoryUsage usage, UsageValue kind) {
        if (usage == null) {
            return -1L;
        }
        switch (kind) {
            case USED:
                return usage.getUsed();
            case COMMITTED:
                return usage.getCommitted();
            case MAX:
                return usage.getMax();
            default:
                return -1L;
        }
    }

    private static String json(String value) {
        StringBuilder result = new StringBuilder("\"");
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            if (character == '\\' || character == '\"') {
                result.append('\\');
            }
            result.append(character);
        }
        return result.append('\"').toString();
    }
}
