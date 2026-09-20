package net.jolink.runtime.jdt;

import java.lang.reflect.Field;
import java.util.ArrayDeque;
import java.util.Queue;

import org.eclipse.core.runtime.Platform;
import org.eclipse.jdt.internal.apt.pluggable.core.dispatch.IdeAnnotationProcessorManager;
import org.eclipse.jdt.internal.compiler.apt.dispatch.ProcessorInfo;
import org.eclipse.jdt.internal.core.JavaModelManager;

/** Initialize the configured factories before the first processor runs. */
public final class InitializedAnnotationProcessorManager extends IdeAnnotationProcessorManager {
    private Queue<ProcessorInfo> initialized;

    @Override
    public void configureFromPlatform(org.eclipse.jdt.internal.compiler.Compiler compiler,
            Object locator, Object project, boolean test) {
        super.configureFromPlatform(compiler, locator, project, test);
        ProcessorConfiguration.restoreFlags((org.eclipse.jdt.core.IJavaProject) project, _processingEnv);
        ProcessorConfiguration.select((org.eclipse.jdt.core.IJavaProject) project);
    }

    @Override
    public ProcessorInfo discoverNextProcessor() {
        if (initialized == null) {
            initialized = new ArrayDeque<>();
            // Keep Eclipse's loading, ProcessingEnvironment, init(), and
            // discovered-processor bookkeeping. Only separate initialization
            // from process(); the original factory order is unchanged.
            for (ProcessorInfo next = super.discoverNextProcessor(); next != null;
                    next = super.discoverNextProcessor()) {
                initialized.add(next);
            }
        }
        return initialized.poll();
    }

    static void install() throws ReflectiveOperationException {
        // JDT 3.25 otherwise picks the first registered extension, with no
        // priority mechanism. Select our subclass explicitly in this Worker.
        Field factory = JavaModelManager.class.getDeclaredField("annotationProcessorManagerFactory");
        factory.setAccessible(true);
        factory.set(JavaModelManager.getJavaModelManager(),
                Platform.getExtensionRegistry().getExtension(
                        "net.jolink.runtime.jdt.worker.initializedProcessors")
                        .getConfigurationElements()[0]);
    }
}
