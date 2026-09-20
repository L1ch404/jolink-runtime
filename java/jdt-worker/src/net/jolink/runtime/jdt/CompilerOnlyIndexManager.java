package net.jolink.runtime.jdt;

import org.eclipse.jdt.internal.core.JavaModelManager;
import org.eclipse.jdt.internal.core.search.indexing.IndexManager;
import org.eclipse.jdt.internal.core.search.processing.IJob;

/** This worker compiles Java; it does not serve JDT workspace searches. */
final class CompilerOnlyIndexManager extends IndexManager {
    private long discardedRequests;

    static void install() {
        JavaModelManager model = JavaModelManager.getJavaModelManager();
        if (model.indexManager instanceof CompilerOnlyIndexManager) return;
        model.indexManager.shutdown();
        model.indexManager = new CompilerOnlyIndexManager();
    }

    @Override
    public synchronized void request(IJob job) {
        // Do not enqueue: merely pausing the old manager retains pending work.
        this.discardedRequests++;
    }

    @Override
    public void reset() {
        // Never start the Java indexing thread, including workspace reopen.
    }

    @Override
    public synchronized int awaitingJobsCount() {
        // No thread is activated and request() never queues work.
        return 0;
    }

    static String metricsJson() {
        IndexManager manager = JavaModelManager.getIndexManager();
        if (!(manager instanceof CompilerOnlyIndexManager)) {
            return "{\"disabled\":false}";
        }
        CompilerOnlyIndexManager compilerOnly = (CompilerOnlyIndexManager) manager;
        synchronized (compilerOnly) {
            return "{\"disabled\":true,\"queued_jobs\":" + compilerOnly.awaitingJobsCount()
                    + ",\"discarded_requests\":" + compilerOnly.discardedRequests + "}";
        }
    }
}
