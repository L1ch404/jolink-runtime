package net.jolink.runtime.jdt;

import java.util.ArrayList;
import java.util.List;

import org.eclipse.jdt.core.IJavaProject;
import org.eclipse.jdt.core.compiler.BuildContext;
import org.eclipse.jdt.core.compiler.CompilationParticipant;

/**
 * Read-only Java Builder observer. It never mutates BuildContext, diagnostics,
 * dependencies, generated files, or class bytes.
 */
public final class CompilationObserver extends CompilationParticipant {
    @Override
    public boolean isActive(IJavaProject project) {
        return BuildObservation.isEnabled()
                && project != null
                && project.getProject().isOpen()
                && "plain-fixture".equals(project.getElementName());
    }

    @Override
    public int aboutToBuild(IJavaProject project) {
        return READY_FOR_BUILD;
    }

    @Override
    public void buildStarting(BuildContext[] files, boolean isBatch) {
        List<String> units = new ArrayList<>();
        if (files != null) {
            for (BuildContext file : files) {
                if (file != null && file.getFile() != null) {
                    units.add(
                            file.getFile().getProjectRelativePath().toString());
                }
            }
        }
        BuildObservation.recordStarting(isBatch, units);
    }

    @Override
    public void buildFinished(IJavaProject project) {
        BuildObservation.recordFinished();
    }

    @Override
    public boolean isAnnotationProcessor() {
        return false;
    }

}
