package net.jolink.runtime.jdt;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** In-memory observations from the read-only compilation participant. */
final class BuildObservation {
    private static boolean enabled;
    private static boolean batchSeen;
    private static boolean incrementalSeen;
    private static boolean buildFinished;
    private static final List<String> compiledUnits = new ArrayList<>();
    private static String barrierBuildId;
    private static boolean barrierReached;
    private static boolean barrierReleased;

    private BuildObservation() {
    }

    static synchronized void setEnabled(boolean value) {
        enabled = value;
    }

    static synchronized boolean isEnabled() {
        return enabled;
    }

    static synchronized void begin() {
        batchSeen = false;
        incrementalSeen = false;
        buildFinished = false;
        compiledUnits.clear();
    }

    static synchronized void armBarrier(String buildId) {
        barrierBuildId = buildId;
        barrierReached = false;
        barrierReleased = false;
    }

    static synchronized void clearBarrier() {
        barrierBuildId = null;
        barrierReached = false;
        barrierReleased = true;
        BuildObservation.class.notifyAll();
    }

    static synchronized boolean barrierReached(String buildId) {
        return buildId != null
                && buildId.equals(barrierBuildId)
                && barrierReached;
    }

    static synchronized void releaseBarrier(String buildId) {
        if (buildId != null && buildId.equals(barrierBuildId)) {
            barrierReleased = true;
            BuildObservation.class.notifyAll();
        }
    }

    private static void awaitBarrierIfArmed() {
        synchronized (BuildObservation.class) {
            if (barrierBuildId == null || barrierReleased) {
                return;
            }
            barrierReached = true;
            BuildObservation.class.notifyAll();
            while (!barrierReleased) {
                try {
                    BuildObservation.class.wait();
                } catch (InterruptedException exception) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
        }
    }

    static synchronized void recordStarting(
            boolean batch, List<String> sourceUnits) {
        if (!enabled) {
            return;
        }
        if (batch) {
            batchSeen = true;
        } else {
            incrementalSeen = true;
        }
        compiledUnits.addAll(sourceUnits);
        awaitBarrierIfArmed();
    }

    static synchronized void recordFinished() {
        if (enabled) {
            buildFinished = true;
        }
    }

    static synchronized Snapshot snapshot() {
        List<String> units = new ArrayList<>(compiledUnits);
        Collections.sort(units);
        return new Snapshot(
                enabled,
                batchSeen,
                incrementalSeen,
                buildFinished,
                Collections.unmodifiableList(units));
    }

    static final class Snapshot {
        final boolean enabled;
        final boolean batchSeen;
        final boolean incrementalSeen;
        final boolean buildFinished;
        final List<String> compiledUnits;

        Snapshot(
                boolean enabled,
                boolean batchSeen,
                boolean incrementalSeen,
                boolean buildFinished,
                List<String> compiledUnits) {
            this.enabled = enabled;
            this.batchSeen = batchSeen;
            this.incrementalSeen = incrementalSeen;
            this.buildFinished = buildFinished;
            this.compiledUnits = compiledUnits;
        }

        String actualBuildKind() {
            if (!enabled) {
                return null;
            }
            if (batchSeen) {
                return "FULL";
            }
            if (incrementalSeen) {
                return "INCREMENTAL";
            }
            return null;
        }

        String buildOutcome() {
            if (!enabled) {
                return "UNVERIFIED";
            }
            if (batchSeen || incrementalSeen) {
                return "COMPILED";
            }
            return "NO_COMPILE";
        }

        boolean callbacksSeen() {
            return batchSeen || incrementalSeen || buildFinished;
        }
    }
}
